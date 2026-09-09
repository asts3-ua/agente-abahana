"""
Interfaz Streamlit para el agente Abahana Villas.

Autenticación: OAuth 2.0 directo con Google (sin Streamlit native auth).
  - Login: st.link_button → Google OAuth → app raíz con ?code=
  - Callback: st.query_params["code"] → intercambio por email → session_state
  - Fallback: Cloud IAP header X-Goog-Authenticated-User-Email
"""

import asyncio
import base64
import hashlib
import hmac
import os
import secrets as _secrets
import time
import urllib.parse
import uuid
from pathlib import Path

import requests
import streamlit as st
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import AGENTS
from conversation_store import get_conversation_store

APP_NAME = "abahana_chat"
ALLOWED_DOMAINS = {"abahanavillas.com", "inferia.io"}

LOGO_PATH = Path(__file__).parent / "assets" / "logo.png"
AGENT_AVATAR_PATH = Path(__file__).parent / "assets" / "agent-concierge.svg"

# Paleta corporativa Abahana Villas
COLOR_BG = "#F5F2E9"
COLOR_PRIMARY = "#1E2B3C"
COLOR_ACCENT = "#7089C4"
COLOR_ACCENT_LIGHT = "#A0B4E0"

# Derivados del mismo eje azul-marino, para roles que la paleta base no cubre:
# texto secundario y enlaces necesitan contraste AA sobre crema y sobre blanco.
COLOR_INK_SOFT = "#4A5768"      # captions, texto de apoyo   (6.6:1 sobre crema)
COLOR_ACCENT_DEEP = "#3E5A9A"   # enlaces, anillo de foco    (6.0:1 sobre crema)
COLOR_SURFACE = "#FFFFFF"
COLOR_SURFACE_ALT = "#FAF7F0"   # burbuja del usuario
COLOR_LINE = "#E2E0D6"          # separadores discretos
COLOR_INK_HOVER = "#2C3E56"
COLOR_ACCENT_WASH = "#E9EEF8"   # fila abierta del histórico (12,2:1 con la tinta)

DOMAIN_ROLES = {
    "abahanavillas.com": "interno",
    "inferia.io": "admin",
}

ROLE_LABELS = {
    "cliente": "Cliente",
    "interno": "Uso Interno",
    "admin": "Administración",
}

WELCOME_MESSAGE = (
    "Hola, soy tu asistente de **Abahana Villas**. Puedo ayudarte a encontrar villas, "
    "consultar información turística de la Costa Blanca y resolver dudas sobre la web corporativa.\n\n"
    "Elige una sugerencia para empezar o escribe tu pregunta abajo."
)

SUGGESTIONS_BY_ROLE: dict[str, list[str]] = {
    "cliente": [
        "Villas con piscina privada para 8 personas en Moraira",
        "¿Cuándo son las fiestas patronales de Calpe?",
        "Villas bien valoradas en Altea con vistas al mar",
        "¿Qué tiempo hace en la Costa Blanca esta semana?",
    ],
    "interno": [
        "Villas disponibles en Jávea con 4 habitaciones",
        "Reservas confirmadas en Moraira este mes",
        "¿Cuándo son las fiestas de Altea este año?",
        "Resumen de reservas por zona en 2026",
    ],
    "admin": [
        "Catálogo de villas con piscina en Calpe",
        "Reservas canceladas en los últimos 30 días",
        "Villas mejor valoradas en Moraira",
        "Resumen de facturación por zona este año",
    ],
}

FEEDBACK_TAG_OPTIONS = {
    "faltan_datos": "Faltan datos",
    "no_entendio_intencion": "No entendió la intención",
    "resultados_poco_relevantes": "Resultados poco relevantes",
    "respuesta_generica": "Respuesta demasiado genérica",
}

FEEDBACK_LABEL_DISPLAY = {
    "util": "Útil",
    "parcial": "Parcial",
    "no_resolvio": "No resolvió",
}

FEEDBACK_RATINGS = {"util": 1, "parcial": 0, "no_resolvio": -1}

HISTORY_LIMIT = 50
HISTORY_TTL_SECONDS = 60
RESUME_MAX_TURNS = 6

@st.cache_resource
def _get_session_service() -> InMemorySessionService:
    return InMemorySessionService()


_session_service = _get_session_service()

# ---------------------------------------------------------------------------
# OAuth config (leído de secrets.toml → [google_oauth])
# ---------------------------------------------------------------------------

def _oauth_cfg() -> dict:
    return st.secrets["google_oauth"]  # type: ignore[index]


def _oauth_is_configured() -> bool:
    """Sin esto, salir de la sesión en local acaba en un traceback de Python:
    la pantalla de login pide secrets.toml y ahí no existe."""
    try:
        cfg = _oauth_cfg()
    except Exception:
        return False
    return all(
        cfg.get(clave)
        for clave in ("client_id", "client_secret", "redirect_uri", "cookie_secret")
    )


# ---------------------------------------------------------------------------
# CSRF state (HMAC-signed, sin almacenamiento server-side)
# ---------------------------------------------------------------------------

def _make_state() -> str:
    nonce = _secrets.token_hex(16)
    ts = str(int(time.time()))
    key = _oauth_cfg()["cookie_secret"].encode()
    sig = hmac.new(key, f"{nonce}:{ts}".encode(), hashlib.sha256).hexdigest()
    raw = f"{nonce}:{ts}:{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _verify_state(state: str) -> bool:
    try:
        padding = "=" * (-len(state) % 4)
        raw = base64.urlsafe_b64decode(state + padding).decode()
        nonce, ts, sig = raw.split(":", 2)
        key = _oauth_cfg()["cookie_secret"].encode()
        expected = hmac.new(key, f"{nonce}:{ts}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and abs(time.time() - int(ts)) < 600
    except Exception:
        return False


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _auth_url() -> str:
    cfg = _oauth_cfg()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "state": _make_state(),
        "prompt": "select_account",
        "access_type": "offline",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def _exchange_code(code: str) -> str:
    """Intercambia código OAuth por email del usuario."""
    cfg = _oauth_cfg()
    token_r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": cfg["redirect_uri"],
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    token_r.raise_for_status()
    access_token = token_r.json()["access_token"]

    info_r = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    info_r.raise_for_status()
    return info_r.json().get("email", "")


# ---------------------------------------------------------------------------
# Identidad
# ---------------------------------------------------------------------------

def _get_user_email() -> str:
    # 0. Cierre de sesión explícito: manda sobre todo lo demás. Sin esto,
    #    TEST_USER_EMAIL volvería a identificar al usuario en el acto y el
    #    botón de cerrar sesión no haría nada visible.
    if st.session_state.get("_logged_out"):
        return ""

    # 1. OAuth propio (almacenado en session_state)
    email = st.session_state.get("_auth_email", "")
    if email:
        return email

    # 2. Cloud IAP: X-Goog-Authenticated-User-Email → "accounts.google.com:user@example.com"
    try:
        iap = st.context.headers.get("X-Goog-Authenticated-User-Email", "")
        if iap:
            return iap.split(":", 1)[-1]
    except Exception:
        pass

    # 3. Desarrollo local
    return os.environ.get("TEST_USER_EMAIL", "")


def _get_role(email: str) -> str:
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return DOMAIN_ROLES.get(domain, "cliente")


# ---------------------------------------------------------------------------
# Callback handler (se llama antes de renderizar la UI)
# ---------------------------------------------------------------------------

def _handle_oauth_callback() -> None:
    """Si la URL tiene ?code=, procesa el callback OAuth y recarga."""
    code = st.query_params.get("code", "")
    state = st.query_params.get("state", "")

    if not code:
        return

    if state and not _verify_state(state):
        st.query_params.clear()
        st.error("Error de seguridad en la autenticación. Intenta de nuevo.")
        st.stop()

    with st.spinner("Verificando sesión..."):
        try:
            email = _exchange_code(code)
            st.session_state["_auth_email"] = email
            # Volver a entrar levanta el cierre de sesión anterior.
            st.session_state.pop("_logged_out", None)
        except Exception as exc:
            st.query_params.clear()
            st.error(f"Error al verificar con Google: {exc}")
            st.stop()

    st.query_params.clear()
    st.rerun()


# ---------------------------------------------------------------------------
# ADK Runner
# ---------------------------------------------------------------------------

def _run_agent(role: str, user_id: str, session_id: str, message: str) -> str:
    agent = AGENTS[role]
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=_session_service)
    content = types.Content(role="user", parts=[types.Part(text=message)])

    async def _run() -> str:
        existing = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if existing is None:
            await _session_service.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
        parts: list[str] = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        parts.append(part.text)
        return "".join(parts)

    return asyncio.run(_run())


def _user_message_before(messages: list[dict], assistant_index: int) -> str:
    for j in range(assistant_index - 1, -1, -1):
        if messages[j].get("role") == "user":
            return messages[j].get("content", "")
    return ""


def _assistant_avatar() -> str | None:
    return str(AGENT_AVATAR_PATH) if AGENT_AVATAR_PATH.exists() else None


def _feedback_context_for_agent(messages: list[dict]) -> str:
    """Contexto de valoraciones de la sesión actual para el siguiente turno."""
    lines: list[str] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        label = msg.get("feedback_label")
        rating = msg.get("rating")
        if label is None and rating is None:
            continue
        question = _user_message_before(messages, i)
        snippet = question[:180] + ("…" if len(question) > 180 else "")
        if label == "util" or rating == 1:
            lines.append(f"✓ Respuesta útil a: «{snippet}»")
        elif label == "parcial" or rating == 0:
            detail = _format_feedback_detail(msg)
            lines.append(f"~ Respuesta parcial a: «{snippet}»{detail}")
        elif label == "no_resolvio" or rating == -1:
            detail = _format_feedback_detail(msg)
            lines.append(f"✗ Respuesta no resolvió: «{snippet}»{detail}")
    if not lines:
        return ""
    return (
        "[Retroalimentación del usuario en esta conversación]\n"
        + "\n".join(lines[-5:])
        + "\nAjusta tu respuesta según esta retroalimentación.\n\n"
    )


def _conversation_context_for_agent(messages: list[dict]) -> str:
    """Transcript compacto para que el agente recupere el hilo al retomarlo.

    La sesión ADK vive en memoria del proceso, así que al abrir una
    conversación antigua el agente no recuerda nada. Se le antepone esto una
    sola vez: a partir del siguiente turno ya tiene los eventos reales.
    """
    turnos: list[str] = []
    for i in range(0, len(messages) - 1):
        if messages[i].get("role") != "user":
            continue
        respuesta = messages[i + 1]
        if respuesta.get("role") != "assistant":
            continue
        pregunta = messages[i].get("content", "")
        contestacion = respuesta.get("content", "")
        turnos.append(
            f"Usuario: {pregunta}\n"
            f"Asistente: {contestacion[:400]}"
            + ("…" if len(contestacion) > 400 else "")
        )
    if not turnos:
        return ""
    return (
        "[Conversación anterior que el usuario está retomando]\n"
        + "\n\n".join(turnos[-RESUME_MAX_TURNS:])
        + "\n[Fin de la conversación anterior]\n\n"
    )


def _format_feedback_detail(msg: dict) -> str:
    parts: list[str] = []
    tags = msg.get("feedback_tags") or []
    if tags:
        tag_labels = [FEEDBACK_TAG_OPTIONS.get(t, t) for t in tags]
        parts.append(f" Motivos: {', '.join(tag_labels)}.")
    comment = (msg.get("feedback_comment") or "").strip()
    if comment:
        parts.append(f" Comentario: «{comment[:120]}».")
    return "".join(parts)


def _save_message_feedback(
    msg: dict,
    *,
    label: str,
    tags: list[str] | None = None,
    comment: str | None = None,
) -> None:
    turn_id = msg.get("turn_id")
    if not turn_id:
        return
    get_conversation_store().save_feedback(
        turn_id,
        label=label,
        tags=tags,
        comment=comment,
    )
    msg["feedback_label"] = label
    msg["feedback_tags"] = tags or []
    msg["feedback_comment"] = (comment or "").strip() or None
    msg["rating"] = FEEDBACK_RATINGS[label]


def _render_assistant_feedback(msg: dict, index: int) -> None:
    turn_id = msg.get("turn_id")
    if not turn_id:
        return

    feedback_key = f"feedback_{turn_id}_{index}"
    draft_key = f"feedback_draft_{turn_id}_{index}"

    if msg.get("feedback_label"):
        label = FEEDBACK_LABEL_DISPLAY.get(msg["feedback_label"], "Enviado")
        st.caption(f"Gracias por tu feedback · {label}")
        return

    # Pulgar abajo: se pregunta por qué antes de guardar.
    if st.session_state.get(draft_key) in ("parcial", "no_resolvio"):
        _render_formulario_feedback(msg, feedback_key, draft_key)
        return

    st.markdown(
        '<div class="feedback-card"><span class="feedback-title">'
        "¿Te ha ayudado esta respuesta?</span></div>",
        unsafe_allow_html=True,
    )
    # st.feedback devuelve 1 para el pulgar arriba y 0 para el de abajo.
    valoracion = st.feedback("thumbs", key=feedback_key)
    if valoracion == 1:
        _save_message_feedback(msg, label="util")
        st.rerun()
    elif valoracion == 0:
        st.session_state[draft_key] = "no_resolvio"
        st.rerun()


def _render_formulario_feedback(msg: dict, feedback_key: str, draft_key: str) -> None:
    """Motivos y comentario tras un pulgar abajo. Ambos opcionales: exigir
    texto para poder valorar reduce el feedback que se recibe."""
    st.markdown(
        '<div class="feedback-card"><span class="feedback-title">'
        "¿Qué ha fallado?</span></div>",
        unsafe_allow_html=True,
    )
    motivos = st.multiselect(
        "Motivos (opcional)",
        options=list(FEEDBACK_TAG_OPTIONS.keys()),
        format_func=lambda k: FEEDBACK_TAG_OPTIONS[k],
        key=f"{feedback_key}_tags",
    )
    comentario = st.text_area(
        "Comentario (opcional)",
        placeholder="¿Qué faltó o qué mejorarías?",
        key=f"{feedback_key}_comment",
        height=80,
    )
    col_enviar, col_cancelar, _ = st.columns([1.2, 1, 1.8])
    with col_enviar:
        if st.button(
            "Enviar",
            key=f"{feedback_key}_send",
            type="primary",
            use_container_width=True,
        ):
            _save_message_feedback(
                msg,
                label=st.session_state[draft_key],
                tags=motivos,
                comment=comentario,
            )
            st.session_state.pop(draft_key, None)
            st.rerun()
    with col_cancelar:
        if st.button(
            "Cancelar",
            key=f"{feedback_key}_cancel",
            use_container_width=True,
        ):
            st.session_state.pop(draft_key, None)
            # Sin esto el widget conservaría el pulgar abajo ya pulsado y el
            # formulario volvería a abrirse en cuanto se repinta.
            st.session_state.pop(feedback_key, None)
            st.rerun()


def _render_chat_history(messages: list[dict]) -> None:
    avatar = _assistant_avatar()
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant":
            with st.chat_message("assistant", avatar=avatar):
                st.markdown(msg["content"])
                _render_assistant_feedback(msg, i)
        else:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])


def _get_suggestions(role: str) -> list[str]:
    return SUGGESTIONS_BY_ROLE.get(role, SUGGESTIONS_BY_ROLE["cliente"])


def _render_welcome_empty_state(role: str) -> None:
    avatar = _assistant_avatar()
    with st.chat_message("assistant", avatar=avatar):
        st.markdown(WELCOME_MESSAGE)
        # Rejilla 2x2: el mensaje de bienvenida ya invita a elegir una,
        # así que no hace falta un rótulo encima.
        suggestions = _get_suggestions(role)
        for row_start in range(0, len(suggestions), 2):
            cols = st.columns(2)
            for col, (i, suggestion) in zip(
                cols,
                enumerate(suggestions[row_start:row_start + 2], start=row_start),
            ):
                with col:
                    if st.button(
                        suggestion,
                        key=f"suggestion_{i}",
                        use_container_width=True,
                    ):
                        st.session_state.pending_prompt = suggestion
                        st.rerun()


def _process_user_prompt(prompt: str, *, role: str, email: str) -> None:
    # Antes de añadir el mensaje nuevo, para que no entre en el transcript.
    resumen = ""
    if st.session_state.pop("resume_context_pending", False):
        resumen = _conversation_context_for_agent(st.session_state.messages)

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    agent_prompt = (
        resumen + _feedback_context_for_agent(st.session_state.messages) + prompt
    )

    error_msg: str | None = None
    started = time.perf_counter()
    avatar = _assistant_avatar()
    with st.chat_message("assistant", avatar=avatar):
        with st.spinner("Consultando..."):
            try:
                response = _run_agent(
                    role=role,
                    user_id=email,
                    session_id=st.session_state.session_id,
                    message=agent_prompt,
                )
                if not response:
                    response = "_(sin respuesta del agente)_"
            except Exception as exc:
                error_msg = str(exc)
                response = f"Error: {exc}"
        st.markdown(response)

    response_ms = int((time.perf_counter() - started) * 1000)
    turn_id = get_conversation_store().save_turn(
        session_id=st.session_state.session_id,
        user_id=email,
        user_role=role,
        user_message=prompt,
        assistant_message=response,
        app_name=APP_NAME,
        response_ms=response_ms,
        error=error_msg,
    )

    st.session_state.messages.append({
        "role": "assistant",
        "content": response,
        "turn_id": turn_id,
    })
    # El turno recién guardado cambia el histórico (conversación nueva, título
    # o recuento), así que la lista cacheada deja de valer.
    _invalidate_history_cache(email)
    st.rerun()


def _new_agent_session(email: str) -> str:
    """Crea una sesión ADK nueva y devuelve su id."""
    session_id = str(uuid.uuid4())
    asyncio.run(
        _session_service.create_session(
            app_name=APP_NAME,
            user_id=email,
            session_id=session_id,
        )
    )
    return session_id


def _clear_conversation_widgets() -> None:
    """Descarta el estado de widgets atado a la conversación que se deja."""
    st.session_state.pop("pending_prompt", None)
    for key in list(st.session_state.keys()):
        if key.startswith(("feedback_", "suggestion_")):
            del st.session_state[key]


def _start_new_conversation(email: str) -> None:
    """Descarta la conversación actual (historial y sesión ADK) y empieza otra."""
    previous_id = st.session_state.get("session_id")
    if previous_id:
        try:
            asyncio.run(
                _session_service.delete_session(
                    app_name=APP_NAME,
                    user_id=email,
                    session_id=previous_id,
                )
            )
        except Exception:
            pass

    _clear_conversation_widgets()
    st.session_state.messages = []
    st.session_state.session_id = _new_agent_session(email)
    st.rerun()


# ---------------------------------------------------------------------------
# Histórico de conversaciones
# ---------------------------------------------------------------------------

@st.cache_data(ttl=HISTORY_TTL_SECONDS, show_spinner=False)
def _cached_sessions(user_id: str, version: int) -> list[dict]:
    """La lista se pinta en cada rerun y en producción el store es BigQuery:
    sin caché sería una consulta facturada por clic.

    `version` entra en la clave de caché a propósito: subirla invalida solo la
    lista de este usuario, sin tirar la caché de los demás. Ojo, no puede
    llamarse `_version`: Streamlit excluye del hash los parámetros que empiezan
    por guion bajo, y entonces la invalidación no haría nada.
    """
    return get_conversation_store().list_sessions(user_id, limit=HISTORY_LIMIT)


@st.cache_resource
def _history_versions() -> dict[str, int]:
    """Versión de la lista por usuario, compartida por todo el proceso.

    Va en cache_resource y no en session_state ni en un dict de módulo:
    en session_state, una pestaña nueva empezaría en la versión 0 y
    reutilizaría lo que otra sesión dejó cacheado ahí; y un dict de módulo se
    reinicia en cada rerun, porque Streamlit reejecuta el script entero.
    """
    return {}


def _history_version(user_id: str) -> int:
    return _history_versions().get(user_id, 0)


def _invalidate_history_cache(user_id: str) -> None:
    versiones = _history_versions()
    versiones[user_id] = versiones.get(user_id, 0) + 1


def _shorten(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _adk_session_exists(email: str, session_id: str) -> bool:
    try:
        return (
            asyncio.run(
                _session_service.get_session(
                    app_name=APP_NAME, user_id=email, session_id=session_id
                )
            )
            is not None
        )
    except Exception:
        return False


def _load_conversation(session_id: str, email: str) -> None:
    """Abre una conversación del histórico y la deja lista para continuar."""
    turnos = get_conversation_store().get_session_turns(session_id, email)
    if not turnos:
        st.warning("No se pudo abrir esa conversación.")
        return

    mensajes: list[dict] = []
    for turno in turnos:
        mensajes.append({"role": "user", "content": turno["user_message"]})
        etiqueta = turno.get("feedback_label")
        mensajes.append(
            {
                "role": "assistant",
                "content": turno["assistant_message"],
                "turn_id": turno["turn_id"],
                "feedback_label": etiqueta,
                "feedback_tags": turno.get("feedback_tags") or [],
                "feedback_comment": turno.get("feedback_comment"),
                "rating": FEEDBACK_RATINGS.get(etiqueta) if etiqueta else None,
            }
        )

    _clear_conversation_widgets()
    st.session_state.messages = mensajes
    st.session_state.session_id = session_id
    # Si la sesión ADK sigue viva en este proceso, el agente ya tiene el hilo;
    # solo hace falta reinyectar el transcript cuando se ha perdido.
    st.session_state.resume_context_pending = not _adk_session_exists(
        email, session_id
    )
    st.rerun()


def _render_conversation_history(email: str) -> None:
    if st.button(
        "＋ Nueva conversación",
        key="new_conversation",
        use_container_width=True,
    ):
        _start_new_conversation(email)

    sesiones = _cached_sessions(email, _history_version(email))
    if not sesiones:
        st.caption("Aquí aparecerán tus conversaciones anteriores.")
        return

    st.caption("Conversaciones")
    actual = st.session_state.get("session_id")
    with st.container():
        for sesion in sesiones:
            session_id = sesion["session_id"]
            es_actual = session_id == actual
            pulsado = st.button(
                _shorten(sesion["title"], 80),
                key=f"conv_{session_id}",
                use_container_width=True,
                type="primary" if es_actual else "secondary",
            )
            if pulsado and not es_actual:
                _load_conversation(session_id, email)


# ---------------------------------------------------------------------------
# UI principal
# ---------------------------------------------------------------------------

# Los tokens van en un f-string corto; el resto del CSS es texto plano que
# los consume vía var(), para no tener que duplicar llaves en 200 líneas.
_BRAND_TOKENS = f"""
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap');

:root {{
    color-scheme: light;
    --abv-bg: {COLOR_BG};
    --abv-surface: {COLOR_SURFACE};
    --abv-surface-alt: {COLOR_SURFACE_ALT};
    --abv-ink: {COLOR_PRIMARY};
    --abv-ink-soft: {COLOR_INK_SOFT};
    --abv-ink-hover: {COLOR_INK_HOVER};
    --abv-accent: {COLOR_ACCENT};
    --abv-accent-deep: {COLOR_ACCENT_DEEP};
    --abv-accent-light: {COLOR_ACCENT_LIGHT};
    --abv-accent-wash: {COLOR_ACCENT_WASH};
    --abv-line: {COLOR_LINE};
}}
"""

_BRAND_CSS = """
html, body, .stApp, [class*="css"] {
    font-family: 'Montserrat', sans-serif !important;
}

/* Legibilidad: el navegador del usuario puede resolver el tema en oscuro.
   Como aquí forzamos superficies claras, hay que forzar el color de texto
   en el mismo sitio; si no, Streamlit lo pinta blanco sobre crema. */
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stBottomBlockContainer"] {
    background-color: var(--abv-bg);
    color: var(--abv-ink);
}

[data-testid="stMarkdownContainer"],
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stMarkdownContainer"] td,
[data-testid="stMarkdownContainer"] th,
[data-testid="stMarkdownContainer"] strong,
.stMarkdown,
label {
    color: var(--abv-ink) !important;
}

h1, h2, h3, h4 {
    color: var(--abv-ink) !important;
    font-weight: 600;
    letter-spacing: 0.02em;
}

[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] p,
[data-testid="stSpinner"] p {
    color: var(--abv-ink-soft) !important;
}

a, a:visited {
    color: var(--abv-accent-deep) !important;
    text-underline-offset: 0.15em;
}

a:hover {
    color: var(--abv-ink) !important;
}

/* El botón de login se centra bajo el logotipo, que también va centrado. */
.login-actions {
    text-align: center;
    margin-top: 1.25rem;
}

/* El enlace de login es un botón: necesita ganar a la regla de enlaces de
   arriba, que si no le pinta el texto del color del propio fondo. */
.login-button,
.login-button:visited,
.login-button:hover {
    display: inline-block;
    padding: 0.65rem 1.5rem;
    background-color: var(--abv-ink);
    color: #FFFFFF !important;
    border-radius: 0.5rem;
    text-decoration: none !important;
    font-weight: 600;
    font-size: 1rem;
    letter-spacing: 0.02em;
}

.login-button:hover {
    background-color: var(--abv-ink-hover);
}

/* Chrome de Streamlit: ocultamos las acciones del toolbar (Deploy, menú)
   pero NO el toolbar entero, porque el botón que despliega la barra
   lateral vive dentro y es la única vía a "Cerrar sesión". */
[data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
#MainMenu,
footer {
    display: none !important;
}

[data-testid="stHeader"] {
    background-color: transparent;
}

[data-testid="stMainBlockContainer"] {
    padding-top: 1.5rem;
}

[data-testid="stSidebar"] {
    background-color: var(--abv-surface);
    border-right: 1px solid var(--abv-line);
}

[data-testid="stSidebar"] * {
    color: var(--abv-ink);
}

/* Quién habla se lee en la superficie, no en un adorno: el agente lleva
   filo azul sobre blanco; el usuario, crema sin filo. */
[data-testid="stChatMessage"] {
    background-color: var(--abv-surface);
    border: 1px solid var(--abv-line);
    border-radius: 0.9rem;
    padding: 1rem 1.15rem;
    color: var(--abv-ink);
}

[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarCustom"]),
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    border-left: 3px solid var(--abv-accent);
}

[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background-color: var(--abv-surface-alt);
}

[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p {
    line-height: 1.65;
}

[data-testid="stChatMessage"] [data-testid="stImage"] img {
    border-radius: 0.5rem;
}

/* Los bordes de tabla del tema son blanco al 10%: invisibles sobre la
   tarjeta blanca cuando el tema resuelve en oscuro. */
[data-testid="stChatMessage"] table th,
[data-testid="stChatMessage"] table td {
    border-color: var(--abv-line) !important;
}

/* El avatar de usuario por defecto de Streamlit viene en rojo; lo pasamos
   a la tinta de marca para no meter un color ajeno a la paleta. */
[data-testid="stChatMessageAvatarUser"] {
    background-color: var(--abv-ink) !important;
    color: #FFFFFF !important;
}

/* Botones. Secundario por defecto; el primario es la única acción
   dominante de la pantalla. Blanco sobre el azul marca da 3.5:1, así que
   el azul se queda en bordes y el relleno pasa a marino (14:1). */
.stButton > button {
    background-color: var(--abv-surface) !important;
    color: var(--abv-ink) !important;
    border: 1px solid var(--abv-accent-light) !important;
    border-radius: 0.5rem !important;
    font-weight: 500 !important;
    letter-spacing: 0.01em;
    transition: background-color .15s ease, border-color .15s ease;
}

.stButton > button:hover {
    background-color: var(--abv-surface-alt) !important;
    border-color: var(--abv-accent) !important;
    color: var(--abv-ink) !important;
}

.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"] {
    background-color: var(--abv-ink) !important;
    color: #FFFFFF !important;
    border-color: var(--abv-ink) !important;
    font-weight: 600 !important;
}

.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover {
    background-color: var(--abv-ink-hover) !important;
    border-color: var(--abv-ink-hover) !important;
}

/* La etiqueta vive dentro de un stMarkdownContainer, que arriba forzamos a
   tinta; sin esto el texto del botón primario queda marino sobre marino. */
.stButton > button[kind="primary"] p,
.stButton > button[kind="primary"] [data-testid="stMarkdownContainer"],
.stButton > button[data-testid="stBaseButton-primary"] p,
.stButton > button[data-testid="stBaseButton-primary"] [data-testid="stMarkdownContainer"] {
    color: #FFFFFF !important;
}

.stButton > button:focus-visible,
a:focus-visible,
[data-baseweb="select"] > div:focus-within {
    outline: 2px solid var(--abv-accent-deep) !important;
    outline-offset: 2px;
}

[data-testid="stSidebar"] [class*="st-key-new_conversation"] button {
    background-color: var(--abv-accent-deep) !important;
    border-color: var(--abv-accent-deep) !important;
    color: #FFFFFF !important;
    font-weight: 600 !important;
    margin-bottom: 0.75rem;
}

[data-testid="stSidebar"] [class*="st-key-new_conversation"] button:hover {
    background-color: var(--abv-ink) !important;
    border-color: var(--abv-ink) !important;
}

[data-testid="stSidebar"] [class*="st-key-new_conversation"] button p,
[data-testid="stSidebar"] [class*="st-key-new_conversation"] button
    [data-testid="stMarkdownContainer"] {
    color: #FFFFFF !important;
}

/* La abierta: tinte azul y filo. Es la única fila marcada de la lista, así
   que se localiza de un vistazo sin necesidad de relleno fuerte. */
[class*="st-key-conv_"] button[kind="primary"],
[class*="st-key-conv_"] button[data-testid="stBaseButton-primary"] {
    background-color: var(--abv-accent-wash) !important;
    border: none !important;
    border-left: 3px solid var(--abv-accent-deep) !important;
    font-weight: 600 !important;
}

[class*="st-key-conv_"] button[kind="primary"]:hover,
[class*="st-key-conv_"] button[data-testid="stBaseButton-primary"]:hover {
    background-color: var(--abv-accent-wash) !important;
    border-left-color: var(--abv-accent-deep) !important;
}

[class*="st-key-conv_"] button[kind="primary"] p,
[class*="st-key-conv_"] button[kind="primary"] [data-testid="stMarkdownContainer"],
[class*="st-key-conv_"] button[data-testid="stBaseButton-primary"] p,
[class*="st-key-conv_"] button[data-testid="stBaseButton-primary"]
    [data-testid="stMarkdownContainer"] {
    color: var(--abv-ink) !important;
}

/* El histórico crece sin límite, así que se le pone techo y se desplaza
   dentro. Se usa max-height y no una altura fija para que con dos
   conversaciones no quede una caja medio vacía. El selector no depende de
   st.container(key=...), que exige una versión de Streamlit más alta que la
   que declara requirements.txt: basta con el bloque que contiene las
   entradas. */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"]:has(
    > [class*="st-key-conv_"]
) {
    max-height: 42vh;
    overflow-y: auto;
    gap: 0.25rem;
    padding-right: 0.4rem;
    /* Con una pista de color (en vez de transparente) el navegador pinta una
       barra permanente en lugar de la superpuesta que macOS oculta sola: es
       la señal de que la lista sigue. Se usan las propiedades estándar y no
       ::-webkit-scrollbar, que Chrome ignora en cuanto hay scrollbar-width. */
    scrollbar-width: thin;
    scrollbar-color: var(--abv-accent-light) var(--abv-surface-alt);
}

/* El histórico es una lista de navegación, no doscientas tarjetas: filas sin
   caja ni borde, y el fondo solo aparece al pasar por encima o en la abierta.
   Así el único relleno de la barra lateral es el botón de acción. */
[class*="st-key-conv_"] button {
    background-color: transparent !important;
    border: none !important;
    border-left: 3px solid transparent !important;
    border-radius: 0.4rem !important;
    min-height: 2.1rem;
    height: auto;
    line-height: 1.35;
    font-size: 0.85rem;
    font-weight: 400 !important;
    justify-content: flex-start;
    padding: 0.35rem 0.55rem !important;
}

[class*="st-key-conv_"] button:hover {
    background-color: var(--abv-surface-alt) !important;
    border-left-color: var(--abv-accent-light) !important;
    color: var(--abv-ink) !important;
}

/* Entre el botón y la etiqueta hay un div sin testid que encoge al ancho del
   texto y queda centrado; sin ensancharlo, los títulos de una línea salen
   centrados y los de dos, a la izquierda. */
[class*="st-key-conv_"] button > div,
[class*="st-key-suggestion_"] button > div {
    width: 100%;
    justify-content: flex-start;
}

/* Una línea por fila: alturas uniformes, se escanean más rápido y caben
   más. El recorte lo decide el ancho real del rail, no un número de letras. */
[class*="st-key-conv_"] button p,
[class*="st-key-conv_"] button [data-testid="stMarkdownContainer"] {
    width: 100%;
    text-align: left !important;
    white-space: nowrap !important;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* Sugerencias: texto largo, deben poder partir en dos líneas sin recortarse.
   Streamlit marca el contenedor de cada widget con .st-key-<key>. */
[class*="st-key-suggestion_"] button {
    min-height: 3.4rem;
    height: auto;
    white-space: normal;
    line-height: 1.35;
    justify-content: flex-start;
    padding: 0.7rem 0.85rem !important;
}

/* Streamlit centra la etiqueta dentro del botón; las preguntas se leen
   mejor alineadas a la izquierda, sobre todo cuando parten en dos líneas. */
[class*="st-key-suggestion_"] button p,
[class*="st-key-suggestion_"] button [data-testid="stMarkdownContainer"] {
    width: 100%;
    text-align: left !important;
    /* Streamlit pone nowrap en la etiqueta: en móvil recortaba la pregunta
       con puntos suspensivos en vez de partirla en dos líneas. */
    white-space: normal !important;
    overflow-wrap: anywhere;
}

/* Entrada de chat. La franja inferior y la superficie del campo son divs
   SIN data-testid que pinta el tema: si este resuelve en oscuro quedaban
   negros bajo una app por lo demás clara. Hay que forzar también el hijo. */
[data-testid="stBottom"],
[data-testid="stBottom"] > div,
[data-testid="stAppScrollToBottomContainer"] {
    background-color: var(--abv-bg) !important;
}

[data-testid="stChatInput"],
[data-testid="stChatInput"] > div {
    background-color: var(--abv-surface) !important;
    border-radius: 0.75rem !important;
}

[data-testid="stChatInput"] {
    border: 1px solid var(--abv-accent-light) !important;
}

[data-testid="stChatInput"] textarea {
    background-color: transparent !important;
}

[data-testid="stChatInputSubmitButton"],
[data-testid="stChatInputSubmitButton"] svg {
    color: var(--abv-ink) !important;
    fill: var(--abv-ink) !important;
}

[data-testid="stChatInput"]:focus-within {
    border-color: var(--abv-accent) !important;
    box-shadow: 0 0 0 2px rgba(112, 137, 196, 0.28) !important;
}

[data-testid="stChatInput"] textarea {
    color: var(--abv-ink) !important;
}

[data-testid="stChatInput"] textarea::placeholder,
[data-testid="stTextArea"] textarea::placeholder {
    color: var(--abv-ink-soft) !important;
    opacity: 1;
}

/* Formulario de feedback */
[data-testid="stTextArea"] textarea,
[data-testid="stTextInput"] input,
[data-baseweb="input"] > div,
[data-baseweb="select"] > div {
    background-color: var(--abv-surface) !important;
    color: var(--abv-ink) !important;
    border: 1px solid var(--abv-accent-light) !important;
}

[data-testid="stMultiSelect"] [role="group"],
[data-testid="stTextAreaRootElement"] {
    background-color: var(--abv-surface) !important;
    border-color: var(--abv-accent-light) !important;
}

/* El texto que se escribe en el multiselect hereda el color del tema
   (casi blanco), así que el marcador quedaba invisible sobre blanco. */
[data-testid="stMultiSelect"] input {
    color: var(--abv-ink) !important;
    -webkit-text-fill-color: var(--abv-ink) !important;
}

[data-testid="stMultiSelect"] input::placeholder {
    color: var(--abv-ink-soft) !important;
    opacity: 1;
}

/* El desplegable se monta fuera del árbol de la app: si el tema resuelve
   en oscuro llega negro aunque el formulario esté claro. */
[data-testid="stMultiSelectDropdown"] {
    background-color: var(--abv-surface) !important;
    border: 1px solid var(--abv-accent-light) !important;
}

/* Las etiquetas elegidas usaban el azul marca con texto blanco (3.5:1);
   el azul profundo las deja en 6.7:1. */
[data-testid="stMultiSelectTag"],
[data-baseweb="tag"] {
    background-color: var(--abv-accent-deep) !important;
    color: #FFFFFF !important;
}

[data-testid="stMultiSelectTag"] * {
    color: #FFFFFF !important;
}

[role="listbox"] [role="option"],
[data-baseweb="popover"] li,
[data-baseweb="popover"] [role="option"] {
    background-color: var(--abv-surface) !important;
    color: var(--abv-ink) !important;
}

[role="listbox"] [role="option"]:hover,
[data-baseweb="popover"] [role="option"]:hover {
    background-color: var(--abv-surface-alt) !important;
}

/* El widget nativo de pulgares trae botones de 24x24: justo el mínimo
   accesible y escasos para el dedo en móvil. Se agrandan y se les da el
   mismo hover discreto que al resto de la interfaz. */
[data-testid="stFeedbackButton"] {
    /* El color del icono lo pone el tema. Si este resuelve en oscuro llega
       casi blanco sobre la tarjeta blanca de la respuesta (1,03:1) y los
       pulgares desaparecen. Hay que forzarlo, como el resto del texto. */
    color: var(--abv-ink-soft) !important;
    /* El botón viene con flex-basis fit-content, que gana al ancho. */
    flex: none !important;
    width: 2.4rem !important;
    height: 2.4rem !important;
    border-radius: 0.5rem !important;
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
}

[data-testid="stFeedbackButton"] svg,
[data-testid="stFeedbackButton"] span {
    color: inherit !important;
    fill: currentColor !important;
}

[data-testid="stFeedbackButton"]:hover {
    background-color: var(--abv-surface-alt) !important;
    color: var(--abv-accent-deep) !important;
}

/* El pulgar elegido queda marcado hasta que se envía. */
[data-testid="stFeedbackButton"][aria-checked="true"] {
    color: var(--abv-accent-deep) !important;
    background-color: var(--abv-accent-wash) !important;
}

/* La pregunta de feedback acompaña a la respuesta, no compite con ella. */
.feedback-card {
    margin-top: 0.85rem;
    padding-top: 0.7rem;
    border-top: 1px solid var(--abv-line);
}

.feedback-title {
    color: var(--abv-ink-soft);
    font-size: 0.8rem;
    font-weight: 500;
}

/* Avisos: fondo propio claro, porque el del tema puede venir oscuro. */
[data-testid="stAlert"] {
    background-color: #EFF2F9 !important;
    border: 1px solid var(--abv-accent-light) !important;
    border-radius: 0.6rem;
}

[data-testid="stAlert"] * {
    color: var(--abv-ink) !important;
}

[data-testid="stAlert"]:has([data-testid="stAlertContentError"]) {
    background-color: #FAEEEC !important;
    border-color: #DDB2AC !important;
}

hr {
    border-color: var(--abv-line) !important;
}

.brand-header {
    text-align: center;
    padding: 0.5rem 0 0.25rem;
}

.brand-header img {
    max-width: 280px;
    width: 100%;
    height: auto;
}

.brand-subtitle {
    color: var(--abv-ink-soft);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    margin-top: 0.6rem;
}

@media (max-width: 640px) {
    .brand-header img {
        max-width: 210px;
    }

    [data-testid="stChatMessage"] {
        padding: 0.85rem 0.9rem;
    }

    /* El botón flotante que abre la barra lateral se comía la primera
       línea de contenido; aquí el ancho no deja sitio a los lados. */
    [data-testid="stMainBlockContainer"] {
        padding-top: 3.25rem;
    }
}

@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        transition-duration: 0.01ms !important;
    }
}
"""


def _inject_brand_css() -> None:
    st.markdown(
        f"<style>{_BRAND_TOKENS}{_BRAND_CSS}</style>",
        unsafe_allow_html=True,
    )


def _render_brand_header(subtitle: str = "Asistente Virtual") -> None:
    if LOGO_PATH.exists():
        logo_b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()
        st.markdown(
            f'<div class="brand-header">'
            f'<img src="data:image/png;base64,{logo_b64}" alt="Abahana Villas" />'
            f'<div class="brand-subtitle">{subtitle}</div>'
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.title("Asistente Abahana Villas")


def main() -> None:
    page_icon = str(LOGO_PATH) if LOGO_PATH.exists() else "🏖️"
    st.set_page_config(
        page_title="Asistente Abahana Villas",
        page_icon=page_icon,
        layout="centered",
        # El histórico es navegación: la barra lateral se abre siempre.
        initial_sidebar_state="expanded",
    )
    _inject_brand_css()

    # Procesar callback OAuth si viene de Google
    _handle_oauth_callback()

    email = _get_user_email()

    # Login obligatorio
    if not email:
        _render_brand_header()
        if not _oauth_is_configured():
            st.error(
                "Falta configurar el acceso con Google: no se encuentra la "
                "sección `[google_oauth]` en `.streamlit/secrets.toml`."
            )
            st.stop()
        st.info("Accede con tu cuenta corporativa de Google.")
        auth_url = _auth_url()
        st.markdown(
            f'<div class="login-actions">'
            f'<a class="login-button" href="{auth_url}" target="_self">'
            "Iniciar sesión con Google</a></div>",
            unsafe_allow_html=True,
        )
        st.stop()

    # Verificar dominio
    domain = email.split("@")[-1].lower()
    if domain not in ALLOWED_DOMAINS:
        _render_brand_header()
        st.error(f"La cuenta **{email}** no tiene acceso. Solo cuentas @abahanavillas.com e @inferia.io.")
        if st.button("Iniciar sesión con otra cuenta"):
            st.session_state.pop("_auth_email", None)
            st.session_state["_logged_out"] = True
            st.rerun()
        st.stop()

    # Inicializar sesión ADK
    if "session_id" not in st.session_state:
        st.session_state.messages = []
        st.session_state.session_id = _new_agent_session(email)

    role = _get_role(email)

    # Cabecera
    _render_brand_header()
    st.caption(f"👤 {email}")

    st.divider()

    # Bienvenida y sugerencias en chat vacío
    if not st.session_state.messages and not st.session_state.get("pending_prompt"):
        _render_welcome_empty_state(role)

    # Historial
    _render_chat_history(st.session_state.messages)

    # Sugerencia clicada desde bienvenida
    if pending := st.session_state.pop("pending_prompt", None):
        _process_user_prompt(pending, role=role, email=email)

    # Input
    if prompt := st.chat_input(
        "Pregunta sobre villas, fiestas, eventos, clima, zonas, amenidades..."
    ):
        _process_user_prompt(prompt, role=role, email=email)

    # Cerrar sesión
    with st.sidebar:
        st.caption(f"Acceso: {ROLE_LABELS.get(role, role)}")
        _render_conversation_history(email)
        st.divider()
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.clear()
            st.session_state["_logged_out"] = True
            st.rerun()


if __name__ == "__main__":
    main()
