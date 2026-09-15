"""Si la sesión del agente se pierde (reinicio o cambio de instancia de Cloud
Run), el agente debe recibir el hilo en vez de empezar de cero en silencio."""

import asyncio
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app

from google.adk.sessions import InMemorySessionService


class _RunnerQueApunta:
    """Sustituye al Runner de ADK y guarda el texto que le llega."""
    recibido: list[str] = []

    def __init__(self, **kwargs):
        pass

    async def run_async(self, *, user_id, session_id, new_message):
        _RunnerQueApunta.recibido.append(new_message.parts[0].text)
        if False:
            yield None


class HiloPerdidoTest(unittest.TestCase):

    def setUp(self):
        _RunnerQueApunta.recibido = []
        self.servicio = InMemorySessionService()
        for p in (patch.object(chat_app, "_session_service", self.servicio),
                  patch.object(chat_app, "Runner", _RunnerQueApunta),
                  patch.dict(chat_app.AGENTS, {"interno": Mock()})):
            p.start()
            self.addCleanup(p.stop)

    def _enviar(self, contexto):
        chat_app._run_agent(role="interno", user_id="u@abahanavillas.com",
                            session_id="s1", message="¿y cuánto cuesta?",
                            contexto_si_se_perdio=contexto)
        return _RunnerQueApunta.recibido[-1]

    def test_si_la_sesion_ya_no_existe_recibe_el_hilo(self):
        texto = self._enviar("[Conversación anterior]\nUsuario: villa ADORA\n")
        self.assertIn("villa ADORA", texto)
        self.assertTrue(texto.endswith("¿y cuánto cuesta?"))

    def test_si_la_sesion_sigue_viva_no_se_repite(self):
        asyncio.run(self.servicio.create_session(
            app_name=chat_app.APP_NAME, user_id="u@abahanavillas.com", session_id="s1"))
        texto = self._enviar("[Conversación anterior]\nUsuario: villa ADORA\n")
        self.assertEqual("¿y cuánto cuesta?", texto)

    def test_una_conversacion_nueva_no_trae_contexto(self):
        self.assertEqual("¿y cuánto cuesta?", self._enviar(""))


class ContextoDelTurnoTest(unittest.TestCase):

    def test_el_contexto_de_rescate_resume_la_pantalla(self):
        mensajes = [
            {"role": "user", "content": "villa ADORA"},
            {"role": "assistant", "content": "ADORA está en Calpe."},
        ]
        contexto = chat_app._conversation_context_for_agent(mensajes)
        self.assertIn("villa ADORA", contexto)
        self.assertIn("ADORA está en Calpe.", contexto)


if __name__ == "__main__":
    unittest.main()
