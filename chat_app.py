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

import requests
import streamlit as st
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import AGENTS

APP_NAME = "abahana_chat"
ALLOWED_DOMAINS = {"abahana.com", "inferia.io"}

DOMAIN_ROLES = {
    "abahana.com": "interno",
    "inferia.io": "admin",
}

ROLE_LABELS = {
    "cliente": "Cliente",
    "interno": "Uso Interno",
    "admin": "Administración",
}

_session_service = InMemorySessionService()

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


# ---------------------------------------------------------------------------
# UI principal
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Asistente Abahana Villas",
        page_icon="🏖️",
        layout="centered",
    )

    # Procesar callback OAuth si viene de Google
    _handle_oauth_callback()

    email = _get_user_email()

    # Login obligatorio
    if not email:
        st.title("Asistente Abahana Villas")
        st.info("Accede con tu cuenta corporativa de Google.")
        auth_url = _auth_url()
        st.markdown(
            f'<a href="{auth_url}" target="_self" style="'
            "display:inline-block;padding:0.4rem 1rem;background-color:#FF4B4B;"
            "color:white;border-radius:0.5rem;text-decoration:none;font-weight:600;"
            'font-size:1rem;">Iniciar sesión con Google</a>',
            unsafe_allow_html=True,
        )
        st.stop()

    # Verificar dominio
    domain = email.split("@")[-1].lower()
    if domain not in ALLOWED_DOMAINS:
        st.title("Asistente Abahana Villas")
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
    st.title("Asistente Abahana Villas")
    st.caption(f"👤 {email}")

    st.divider()

    # Historial
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input
    if prompt := st.chat_input("Pregunta sobre villas, zonas, amenidades..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Consultando..."):
                try:
                    response = _run_agent(
                        role=role,
                        user_id=email,
                        session_id=st.session_state.session_id,
                        message=prompt,
                    )
                    if not response:
                        response = "_(sin respuesta del agente)_"
                except Exception as exc:
                    response = f"Error: {exc}"
            st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})

    # Cerrar sesión
    with st.sidebar:
        if st.button("Cerrar sesión"):
            st.session_state.clear()
            st.rerun()


if __name__ == "__main__":
    main()
