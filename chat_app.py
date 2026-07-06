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
ALLOWED_DOMAINS = {"abahana.com", "inferia.io"}

LOGO_PATH = Path(__file__).parent / "assets" / "logo.png"
AGENT_AVATAR_PATH = Path(__file__).parent / "assets" / "agent-concierge.svg"

# Paleta corporativa Abahana Villas
COLOR_BG = "#F5F2E9"
COLOR_PRIMARY = "#1E2B3C"
COLOR_ACCENT = "#7089C4"
COLOR_ACCENT_LIGHT = "#A0B4E0"

DOMAIN_ROLES = {
    "abahana.com": "interno",
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

@st.cache_resource
def _get_session_service() -> InMemorySessionService:
    return InMemorySessionService()


_session_service = _get_session_service()

# ---------------------------------------------------------------------------
# OAuth config (leído de secrets.toml → [google_oauth])
# ---------------------------------------------------------------------------

def _oauth_cfg() -> dict:
    return st.secrets["google_oauth"]  # type: ignore[index]


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
    msg["rating"] = {"util": 1, "parcial": 0, "no_resolvio": -1}[label]


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

    draft_label = st.session_state.get(draft_key)
    if draft_label in ("parcial", "no_resolvio"):
        st.markdown(
            '<div class="feedback-card"><span class="feedback-title">'
            "Cuéntanos un poco más</span></div>",
            unsafe_allow_html=True,
        )
        selected_tags = st.multiselect(
            "Motivos (opcional)",
            options=list(FEEDBACK_TAG_OPTIONS.keys()),
            format_func=lambda k: FEEDBACK_TAG_OPTIONS[k],
            key=f"{feedback_key}_tags",
        )
        comment = st.text_area(
            "Comentario (opcional)",
            placeholder="¿Qué faltó o qué mejorarías?",
            key=f"{feedback_key}_comment",
            height=80,
        )
        col_send, col_cancel = st.columns([1, 1])
        with col_send:
            if st.button("Enviar feedback", key=f"{feedback_key}_send", type="primary"):
                _save_message_feedback(
                    msg,
                    label=draft_label,
                    tags=selected_tags,
                    comment=comment,
                )
                st.session_state.pop(draft_key, None)
                st.rerun()
        with col_cancel:
            if st.button("Cancelar", key=f"{feedback_key}_cancel"):
                st.session_state.pop(draft_key, None)
                st.rerun()
        return

    st.markdown(
        '<div class="feedback-card"><span class="feedback-title">'
        "¿Te ha ayudado esta respuesta?</span></div>",
        unsafe_allow_html=True,
    )
    col_util, col_parcial, col_no = st.columns(3)
    with col_util:
        if st.button("Útil", key=f"{feedback_key}_util", use_container_width=True):
            _save_message_feedback(msg, label="util")
            st.rerun()
    with col_parcial:
        if st.button("Parcial", key=f"{feedback_key}_parcial", use_container_width=True):
            st.session_state[draft_key] = "parcial"
            st.rerun()
    with col_no:
        if st.button("No resolvió", key=f"{feedback_key}_no", use_container_width=True):
            st.session_state[draft_key] = "no_resolvio"
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
        st.markdown("**Sugerencias para empezar**")
        for i, suggestion in enumerate(_get_suggestions(role)):
            if st.button(suggestion, key=f"suggestion_{i}", use_container_width=True):
                st.session_state.pending_prompt = suggestion
                st.rerun()


def _process_user_prompt(prompt: str, *, role: str, email: str) -> None:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    agent_prompt = _feedback_context_for_agent(st.session_state.messages) + prompt

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
    st.rerun()


# ---------------------------------------------------------------------------
# UI principal
# ---------------------------------------------------------------------------

def _inject_brand_css() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap');

        html, body, [class*="css"] {{
            font-family: 'Montserrat', sans-serif !important;
        }}

        .stApp {{
            background-color: {COLOR_BG};
        }}

        [data-testid="stHeader"] {{
            background-color: {COLOR_BG};
        }}

        [data-testid="stSidebar"] {{
            background-color: #FFFFFF;
            border-right: 1px solid {COLOR_ACCENT_LIGHT};
        }}

        h1, h2, h3, [data-testid="stMarkdownContainer"] p strong {{
            color: {COLOR_PRIMARY} !important;
            font-weight: 600;
            letter-spacing: 0.04em;
        }}

        [data-testid="stCaptionContainer"] p {{
            color: {COLOR_ACCENT} !important;
        }}

        [data-testid="stChatMessage"] {{
            background-color: #FFFFFF;
            border: 1px solid {COLOR_ACCENT_LIGHT};
            border-radius: 0.75rem;
        }}

        [data-testid="stChatInput"] textarea {{
            border-color: {COLOR_ACCENT_LIGHT} !important;
        }}

        [data-testid="stChatInput"] textarea:focus {{
            border-color: {COLOR_ACCENT} !important;
            box-shadow: 0 0 0 1px {COLOR_ACCENT} !important;
        }}

        .stButton > button {{
            background-color: {COLOR_ACCENT} !important;
            color: white !important;
            border: none !important;
            border-radius: 0.5rem !important;
            font-weight: 600 !important;
            letter-spacing: 0.03em;
        }}

        .stButton > button:hover {{
            background-color: {COLOR_PRIMARY} !important;
        }}

        [data-testid="stAlert"] {{
            border-radius: 0.5rem;
        }}

        hr {{
            border-color: {COLOR_ACCENT_LIGHT} !important;
        }}

        #MainMenu, footer, header[data-testid="stHeader"] {{
            visibility: hidden;
        }}

        .brand-header {{
            text-align: center;
            padding: 1.5rem 0 0.5rem;
        }}

        .brand-header img {{
            max-width: 320px;
            width: 100%;
            height: auto;
        }}

        .brand-subtitle {{
            color: {COLOR_PRIMARY};
            font-size: 1.1rem;
            font-weight: 500;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            margin-top: 0.75rem;
        }}

        .feedback-card {{
            margin-top: 0.5rem;
            padding: 0.65rem 0.85rem;
            background: #F8F6F1;
            border: 1px solid {COLOR_ACCENT_LIGHT};
            border-radius: 0.6rem;
        }}

        .feedback-title {{
            color: {COLOR_PRIMARY};
            font-size: 0.85rem;
            font-weight: 600;
        }}

        [data-testid="stChatMessage"] [data-testid="stImage"] img {{
            border-radius: 0.5rem;
        }}
        </style>
        """,
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
    )
    _inject_brand_css()

    # Procesar callback OAuth si viene de Google
    _handle_oauth_callback()

    email = _get_user_email()

    # Login obligatorio
    if not email:
        _render_brand_header()
        st.info("Accede con tu cuenta corporativa de Google.")
        auth_url = _auth_url()
        st.markdown(
            f'<a href="{auth_url}" target="_self" style="'
            f"display:inline-block;padding:0.6rem 1.5rem;background-color:{COLOR_ACCENT};"
            "color:white;border-radius:0.5rem;text-decoration:none;font-weight:600;"
            f'font-size:1rem;letter-spacing:0.03em;font-family:Montserrat,sans-serif;">'
            "Iniciar sesión con Google</a>",
            unsafe_allow_html=True,
        )
        st.stop()

    # Verificar dominio
    domain = email.split("@")[-1].lower()
    if domain not in ALLOWED_DOMAINS:
        _render_brand_header()
        st.error(f"La cuenta **{email}** no tiene acceso. Solo cuentas @abahana.com e @inferia.io.")
        if st.button("Iniciar sesión con otra cuenta"):
            st.session_state.pop("_auth_email", None)
            st.rerun()
        st.stop()

    # Inicializar sesión ADK
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.messages = []
        asyncio.run(
            _session_service.create_session(
                app_name=APP_NAME,
                user_id=email,
                session_id=st.session_state.session_id,
            )
        )

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
        if role == "admin":
            stored = get_conversation_store().count_turns()
            if stored is not None:
                st.caption(f"Turnos almacenados: {stored:,}")
            ratings = get_conversation_store().count_ratings()
            if ratings:
                st.caption(
                    f"Útil {ratings['util']:,}  ·  "
                    f"Parcial {ratings['parcial']:,}  ·  "
                    f"No resolvió {ratings['no_resolvio']:,}"
                )
        if st.button("Cerrar sesión"):
            st.session_state.clear()
            st.rerun()


if __name__ == "__main__":
    main()
