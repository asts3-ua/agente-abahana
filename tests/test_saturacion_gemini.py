"""Cuando Gemini está saturado (429 RESOURCE_EXHAUSTED) se reintenta, y si aun
así falla el usuario ve un mensaje comprensible, no el error de Google."""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app

from google.adk.models import Gemini


class ReintentosTest(unittest.TestCase):

    def test_los_tres_agentes_reintentan_ante_saturacion(self):
        for rol, ag in agent.AGENTS.items():
            self.assertIsInstance(ag.model, Gemini, rol)
            reintentos = ag.model.retry_options
            self.assertIsNotNone(reintentos, rol)
            self.assertGreaterEqual(reintentos.attempts, 3, rol)
            self.assertIn(429, reintentos.http_status_codes, rol)
            self.assertEqual("gemini-2.5-flash", ag.model.model, rol)

    def test_la_busqueda_en_internet_tambien(self):
        cliente = Mock()
        cliente.models.generate_content.return_value = Mock(text="ok", candidates=[])
        with patch.object(agent, "_get_genai_client", return_value=cliente):
            agent.buscar_internet("fiestas de Calpe")
        config = cliente.models.generate_content.call_args.kwargs["config"]
        self.assertIn(429, config.http_options.retry_options.http_status_codes)


class MensajeDeErrorTest(unittest.TestCase):

    def test_saturacion_en_palabras(self):
        error = RuntimeError("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, "
                             "'message': 'Resource exhausted. Please try again later.'}}")
        texto = chat_app._mensaje_error(error)
        self.assertIn("vuelve a intentarlo", texto)
        self.assertNotIn("RESOURCE_EXHAUSTED", texto)

    def test_otros_errores_siguen_mostrandose(self):
        self.assertIn("tabla no encontrada", chat_app._mensaje_error(RuntimeError("tabla no encontrada")))


if __name__ == "__main__":
    unittest.main()
