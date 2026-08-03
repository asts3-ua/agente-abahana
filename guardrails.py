"""
Guardarailes del agente de Abahana Villas.

Dos guardarailes de ENTRADA (antes de llamar al modelo), combinados en
`build_before_model_callback`:

  1. `check_jailbreak`   — regex puro, determinista, sin red. Bloquea intentos
                            de anular/revelar las instrucciones del sistema.
  2. `classify_topic`    — clasificador LLM barato (mismo modelo del agente)
                            que decide si el mensaje entra en el ámbito de
                            Abahana Villas (villas, reservas, web corporativa,
                            turismo Costa Blanca). Fail-open ante errores de
                            infraestructura para no tumbar el agente entero
                            por un fallo de red/cuota ajeno al guardarail.

Y un guardarail de HERRAMIENTA:

  3. `check_sql_scope`   — valida que las queries de `ejecutar_sql` sean de
                            solo lectura, una única sentencia, y limitadas al
                            dataset del proyecto (evita exfiltrar datos de
                            otros datasets/proyectos de la misma cuenta GCP
                            vía prompt injection). Es una capa adicional de
                            defensa en profundidad: el límite real lo pone el
                            IAM del service account, esto reduce superficie.

Todas las funciones de decisión son puras (sin llamadas de red) salvo
`classify_topic`, para poder testearlas sin credenciales de GCP.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types as genai_types

log = logging.getLogger("agente-villas.guardrails")

REFUSAL_MESSAGE = (
    "Soy el asistente virtual de Abahana Villas y solo puedo ayudarte con "
    "villas vacacionales, reservas, la web corporativa e información "
    "turística de la Costa Blanca. No puedo ayudarte con eso. "
    "¿Te ayudo a buscar una villa o resolver alguna duda sobre tu estancia?"
)

_TOPIC_MODEL = "gemini-2.5-flash"

# ---------------------------------------------------------------------------
# Capa 1 — jailbreak / prompt injection (regex, determinista, fail-closed)
# ---------------------------------------------------------------------------

_JAILBREAK_PATTERNS = [
    r"ignora?\s+(todas?\s+)?(las?\s+)?instruccion",
    r"olvida\s+(todas?\s+)?(las?\s+)?instruccion",
    r"(muestra|revela|imprime|repite|dime)\s+.*(system prompt|prompt del sistema|"
    r"tus?\s+instruccion|tu\s+configuraci[oó]n)",
    r"eres\s+ahora\s+",
    r"a\s?partir\s+de\s+ahora\s+(eres|ser[aá]s|act[uú]a)",
    r"act[uú]a\s+como\s+(si\s+fueras\s+)?(otro|un|una)\b",
    r"finge\s+(que\s+)?(eres|ser)",
    r"pretend\s+(you\s+are|to\s+be)",
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"reveal\s+(your\s+)?(system\s+)?prompt",
    r"print\s+(your\s+)?(system\s+)?instructions",
    r"repeat\s+(the\s+)?(words|text)\s+above",
    r"\bdan\s+mode\b",
    r"developer\s+mode",
    r"modo\s+desarrollador",
    r"sin\s+(ninguna\s+)?restricci",
    r"sin\s+filtro",
    r"no\s+filter",
    r"jailbreak",
    r"bypass\s+(tus?\s+)?(restricciones|reglas|instrucciones|filtros)",
]
_JAILBREAK_RE = re.compile("|".join(_JAILBREAK_PATTERNS), re.IGNORECASE)


def check_jailbreak(texto: str) -> bool:
    """True si el texto intenta manipular/anular las instrucciones del agente."""
    return bool(_JAILBREAK_RE.search(texto))


# ---------------------------------------------------------------------------
# Capa 2 — ámbito temático (clasificador LLM, fail-open)
# ---------------------------------------------------------------------------

_TOPIC_SYSTEM_INSTRUCTION = """
Clasificas si un mensaje de usuario entra dentro del ámbito de un asistente
virtual de Abahana Villas (alquiler vacacional de villas en la Costa Blanca,
España).

DENTRO del ámbito:
- Villas: catálogo, características, disponibilidad, amenidades, ratings.
- Reservas: fechas, importes, estados, estadísticas.
- Información corporativa de Abahana Villas (política de privacidad, aviso
  legal, condiciones de alquiler, contacto, oficinas).
- Turismo local de la Costa Blanca: fiestas, eventos, clima, atracciones,
  horarios de pueblos como Altea, Calpe, Moraira, Dénia, Benidorm.
- Saludos, agradecimientos y preguntas de seguimiento sobre la conversación
  o sobre el propio asistente (qué puede hacer, cómo funciona a alto nivel).
- Preguntas neutras de utilidad que son necesarias para calcular fechas de
  estancia o disponibilidad (p. ej. "qué día es hoy", "qué día de la semana
  es el 15 de agosto").

FUERA del ámbito: cualquier otro tema (programación, matemáticas, deportes,
noticias generales, otras empresas, consejos médicos/legales/financieros no
relacionados con el alquiler, cultura general no turística, contenido
personal, recetas, traducciones no relacionadas, etc.)

Responde ÚNICAMENTE con una palabra en mayúsculas: DENTRO o FUERA.
""".strip()


def _get_fresh_user_text(llm_request: LlmRequest) -> Optional[str]:
    """Extrae el texto del último turno de usuario, si es un turno nuevo.

    Devuelve None cuando el último `content` no es un mensaje de usuario
    recién escrito (p. ej. es un resultado de herramienta / continuación
    interna del bucle de function-calling), para no re-gatear esas
    continuaciones.
    """
    contents = llm_request.contents
    if not contents:
        return None

    last = contents[-1]
    if last.role != "user":
        return None

    parts = last.parts or []
    if not parts:
        return None

    # Un turno de resultado de herramienta trae function_response, no texto
    # escrito por el usuario: no debe re-gatearse.
    if any(getattr(p, "function_response", None) is not None for p in parts):
        return None

    textos = [p.text for p in parts if getattr(p, "text", None)]
    if not textos:
        return None

    return "\n".join(textos).strip()


def classify_topic(texto: str, genai_client) -> bool:
    """True si el texto está dentro del ámbito de Abahana Villas.

    Ante cualquier fallo (red, cuota, respuesta inesperada) se asume DENTRO
    (fail-open): un guardarail de ámbito no debe tumbar el agente completo
    por un problema de infraestructura ajeno a él. El guardarail de
    jailbreak (fail-closed) sigue aplicando siempre.
    """
    try:
        response = genai_client.models.generate_content(
            model=_TOPIC_MODEL,
            contents=texto[:2000],
            config=genai_types.GenerateContentConfig(
                system_instruction=_TOPIC_SYSTEM_INSTRUCTION,
                temperature=0,
                max_output_tokens=20,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception:
        log.exception("classify_topic: fallo al clasificar ámbito, fail-open")
        return True

    texto_respuesta = (getattr(response, "text", None) or "").strip().upper()
    return "FUERA" not in texto_respuesta


def _refusal_response() -> LlmResponse:
    return LlmResponse(
        content=genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=REFUSAL_MESSAGE)],
        ),
    )


def build_before_model_callback(
    genai_client_factory: Callable[[], "object"],
):
    """Construye el `before_model_callback` para pasar a `Agent(...)`.

    `genai_client_factory` se invoca de forma perezosa (solo si hace falta
    clasificar) para reutilizar el mismo cliente genai que usa `buscar_internet`.
    """

    def before_model_callback(callback_context, llm_request: LlmRequest):
        texto = _get_fresh_user_text(llm_request)
        if not texto:
            return None  # continuación interna (p. ej. tras una tool) — no gatear

        if check_jailbreak(texto):
            log.warning("Guardarail: intento de jailbreak/inyección bloqueado: %r", texto[:200])
            return _refusal_response()

        try:
            client = genai_client_factory()
            dentro_de_ambito = classify_topic(texto, client)
        except Exception:
            log.exception("Guardarail: no se pudo obtener el cliente de clasificación, fail-open")
            dentro_de_ambito = True

        if not dentro_de_ambito:
            log.info("Guardarail: mensaje fuera de ámbito bloqueado: %r", texto[:200])
            return _refusal_response()

        return None

    return before_model_callback


# ---------------------------------------------------------------------------
# Capa 3 — alcance de `ejecutar_sql` (solo lectura, un dataset, una sentencia)
# ---------------------------------------------------------------------------

_SQL_BLOCKED_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"CALL|EXECUTE|DECLARE|LOAD|EXPORT|IMPORT)\b",
    re.IGNORECASE,
)

# Referencias `proyecto.dataset.tabla` o `dataset.tabla` tras FROM/JOIN,
# con o sin backticks.
_SQL_TABLE_REF_RE = re.compile(
    r"(?:FROM|JOIN)\s+`?([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)(?:\.[a-zA-Z0-9_-]+)?`?",
    re.IGNORECASE,
)


def check_sql_scope(query: str, project_id: str, dataset: str) -> Optional[str]:
    """Valida que `query` sea una única SELECT/WITH de solo lectura limitada
    a `project_id.dataset`.

    Devuelve None si la query es válida, o un mensaje de error en español
    (apto para mostrar al usuario) si debe rechazarse.
    """
    stripped = query.strip()
    if not stripped:
        return "La consulta está vacía."

    normalized = stripped.upper()
    if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
        return "Solo se permiten consultas SELECT o WITH."

    # Permite un ';' final opcional, pero no sentencias apiladas.
    sin_punto_final = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in sin_punto_final:
        return "No se permiten múltiples sentencias separadas por ';'."

    if _SQL_BLOCKED_KEYWORDS_RE.search(stripped):
        return "La consulta contiene una palabra clave no permitida (solo lectura)."

    for match in _SQL_TABLE_REF_RE.finditer(stripped):
        primero, segundo = match.group(1), match.group(2)
        es_dos_partes = match.group(0).count(".") == 1
        if es_dos_partes:
            # `dataset.tabla` — el proyecto es implícito (el del cliente BigQuery).
            ref_dataset = primero
        else:
            # `proyecto.dataset.tabla[.algo]`
            ref_proyecto, ref_dataset = primero, segundo
            if ref_proyecto.upper() != project_id.upper():
                return f"Solo se permite consultar el proyecto '{project_id}'."

        if ref_dataset.upper() != dataset.upper():
            return f"Solo se permite consultar el dataset '{dataset}'."

    return None
