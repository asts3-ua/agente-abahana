"""
Verificación de fidelidad (groundedness) de las respuestas del agente.

No mide "verdad absoluta del mundo": mide si lo que dice el chatbot está
soportado por la evidencia de las herramientas de ese turno (BigQuery, web,
búsqueda). El resultado se guarda para analítica y se muestra solo al equipo,
nunca dentro del texto de la respuesta al usuario.

Veredictos:
  soportada      — las afirmaciones factibles encajan con las tools
  parcial        — mezcla de datos soportados e inventados/contradichos
  no_soportada   — contradice las tools o inventa hechos clave
  sin_evidencia  — afirma hechos sin haber usado tools
  conversacional — saludo, rechazo de ámbito, meta-pregunta; nada que auditar
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from google.genai import types as genai_types

log = logging.getLogger("agente-villas.groundedness")

_JUDGE_MODEL = "gemini-2.5-flash"
_MAX_EVIDENCE_CHARS = 8000
_MAX_RESULT_CHARS = 2500
_MAX_TRACE_STORED_CHARS = 20000

VERDICTS = (
    "soportada",
    "parcial",
    "no_soportada",
    "sin_evidencia",
    "conversacional",
)

PROBLEM_VERDICTS = frozenset({"parcial", "no_soportada", "sin_evidencia"})

_JUDGE_SYSTEM_INSTRUCTION = """
Eres un auditor de fidelidad de un chatbot de Abahana Villas (alquiler de
villas en la Costa Blanca). Comparas la RESPUESTA del asistente con la
EVIDENCIA de las herramientas usadas en ese mismo turno.

Reglas:
- La evidencia de herramientas es la fuente de verdad de este turno.
- No uses conocimiento propio para "corregir" la evidencia.
- Números, nombres de villas, capacidades, fechas, importes, disponibilidad
  y valoraciones DEBEN coincidir con la evidencia (o ser un resumen fiel).
- Parafrasear está bien. Inventar villas, cifras o fechas no lo está.
- Si las tools devolvieron 0 resultados y el asistente dice que no hay
  coincidencias, eso es SOPORTADA.
- Si el asistente se niega a salir de ámbito, saluda, o solo pide
  aclaración, es CONVERSACIONAL.
- Si afirma hechos (villas, fiestas, clima, reservas) SIN evidencia de
  tools, es SIN_EVIDENCIA.
- Si mezcla datos correctos con otros no presentes o contradictorios, es PARCIAL.
- Si el núcleo de la respuesta contradice las tools o está inventado, es
  NO_SOPORTADA.
- Citas de fuentes de internet cuentan como soportadas si aparecen en la
  evidencia de `buscar_internet` o `consultar_web`.

Responde ÚNICAMENTE un JSON con esta forma:
{
  "veredicto": "soportada|parcial|no_soportada|sin_evidencia|conversacional",
  "confianza": 0.0,
  "afirmaciones_no_soportadas": ["..."],
  "resumen": "una frase en español"
}
confianza es un número entre 0 y 1.
afirmaciones_no_soportadas: lista corta (máx. 5) de hechos no respaldados.
Si no hay ninguno, usa [].
""".strip()


@dataclass
class ToolEvent:
    kind: str  # "call" | "result"
    name: str
    payload: Any = None


@dataclass
class GroundednessResult:
    verdict: str
    score: Optional[float] = None
    summary: str = ""
    ungrounded_claims: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_store_fields(self) -> dict[str, Any]:
        return {
            "groundedness_verdict": self.verdict,
            "groundedness_score": self.score,
            "groundedness_summary": self.summary or None,
            "ungrounded_claims": json.dumps(
                self.ungrounded_claims, ensure_ascii=False
            ),
            "tools_used": ",".join(self.tools_used) or None,
            "tool_trace": _truncate(
                json.dumps(self.tool_trace, ensure_ascii=False, default=str),
                _MAX_TRACE_STORED_CHARS,
            ),
        }

    def to_ui_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "summary": self.summary,
            "ungrounded_claims": self.ungrounded_claims,
            "tools_used": self.tools_used,
            "error": self.error,
        }


def extract_tool_events(event: Any) -> list[ToolEvent]:
    """Extrae llamadas y resultados de tools de un evento ADK."""
    out: list[ToolEvent] = []
    getter_calls = getattr(event, "get_function_calls", None)
    getter_resps = getattr(event, "get_function_responses", None)
    if callable(getter_calls):
        for call in getter_calls() or []:
            out.append(
                ToolEvent(
                    kind="call",
                    name=getattr(call, "name", "") or "",
                    payload=_jsonable(getattr(call, "args", None)),
                )
            )
    if callable(getter_resps):
        for resp in getter_resps() or []:
            out.append(
                ToolEvent(
                    kind="result",
                    name=getattr(resp, "name", "") or "",
                    payload=_compact_payload(getattr(resp, "response", None)),
                )
            )
    return out


def tool_events_to_trace(events: list[ToolEvent]) -> list[dict[str, Any]]:
    return [asdict(e) for e in events]


def tools_used_from_trace(trace: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in trace:
        name = (item.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def format_evidence(trace: list[dict[str, Any]]) -> str:
    if not trace:
        return "(sin herramientas en este turno)"
    blocks: list[str] = []
    for item in trace:
        kind = item.get("kind", "?")
        name = item.get("name", "?")
        payload = item.get("payload")
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        serialized = _truncate(serialized, _MAX_RESULT_CHARS)
        blocks.append(f"[{kind}] {name}: {serialized}")
    return _truncate("\n".join(blocks), _MAX_EVIDENCE_CHARS)


def parse_judge_payload(texto: str) -> dict[str, Any]:
    """Parsea el JSON del juez, tolerando fences markdown."""
    raw = (texto or "").strip()
    if not raw:
        raise ValueError("respuesta vacía del juez")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no hay JSON en la respuesta del juez")
        raw = raw[start : end + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("el JSON del juez no es un objeto")
    return data


def result_from_judge_payload(
    data: dict[str, Any],
    *,
    tools_used: list[str],
    tool_trace: list[dict[str, Any]],
) -> GroundednessResult:
    verdict = str(data.get("veredicto") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"veredicto desconocido: {verdict!r}")

    score = data.get("confianza")
    try:
        score_f = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_f = None
    if score_f is not None:
        score_f = max(0.0, min(1.0, score_f))

    claims = data.get("afirmaciones_no_soportadas") or []
    if not isinstance(claims, list):
        claims = [str(claims)]
    claims = [str(c).strip() for c in claims if str(c).strip()][:5]

    summary = str(data.get("resumen") or "").strip()[:400]
    return GroundednessResult(
        verdict=verdict,
        score=score_f,
        summary=summary,
        ungrounded_claims=claims,
        tools_used=tools_used,
        tool_trace=tool_trace,
    )


def judge_groundedness(
    *,
    question: str,
    answer: str,
    tool_trace: list[dict[str, Any]],
    genai_client,
) -> GroundednessResult:
    """Llama al modelo juez. Fail-open: si falla, verdict=error no bloquea el chat."""
    tools_used = tools_used_from_trace(tool_trace)
    evidence = format_evidence(tool_trace)
    prompt = (
        f"PREGUNTA:\n{question.strip()[:2000]}\n\n"
        f"RESPUESTA:\n{answer.strip()[:4000]}\n\n"
        f"EVIDENCIA DE HERRAMIENTAS:\n{evidence}"
    )
    try:
        response = genai_client.models.generate_content(
            model=_JUDGE_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_JUDGE_SYSTEM_INSTRUCTION,
                temperature=0,
                max_output_tokens=400,
                response_mime_type="application/json",
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        payload = parse_judge_payload(getattr(response, "text", None) or "")
        return result_from_judge_payload(
            payload, tools_used=tools_used, tool_trace=tool_trace
        )
    except Exception as exc:
        log.exception("juez de fidelidad: fallo, se registra como error")
        return GroundednessResult(
            verdict="error",
            summary="No se pudo verificar la fidelidad de la respuesta.",
            tools_used=tools_used,
            tool_trace=tool_trace,
            error=str(exc),
        )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _compact_payload(value: Any) -> Any:
    """Reduce resultados grandes (listas de villas) para el juez y el almacén."""
    value = _jsonable(value)
    if isinstance(value, dict):
        compacted = dict(value)
        for key in ("matches", "reservas", "rows", "resultados"):
            items = compacted.get(key)
            if isinstance(items, list) and len(items) > 8:
                compacted[key] = items[:8]
                compacted[f"{key}_omitidos"] = len(items) - 8
        return compacted
    if isinstance(value, list) and len(value) > 8:
        return value[:8] + [f"... ({len(value) - 8} omitidos)"]
    return value


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "…[truncado]"
