"""El agente usa Gemini 3.8 Flash, que solo existe en el punto de acceso global
de Vertex AI (decisión del usuario, sabiendo que las peticiones pueden
procesarse fuera de la UE)."""

import os
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class ModeloTest(unittest.TestCase):

    def test_los_tres_roles_usan_gemini_3_8_flash(self):
        for rol, ag in agent.AGENTS.items():
            self.assertEqual("gemini-3.8-flash", ag.model, rol)

    def test_la_busqueda_en_internet_tambien(self):
        cliente = Mock()
        cliente.models.generate_content.return_value = Mock(text="ok", candidates=[])
        with patch.object(agent, "_get_genai_client", return_value=cliente):
            agent.buscar_internet("fiestas de Calpe")
        self.assertEqual("gemini-3.8-flash", cliente.models.generate_content.call_args.kwargs["model"])

    def test_razonamiento_al_minimo_que_admite(self):
        # 3.8 tampoco deja apagarlo ni admite MINIMAL (comprobado contra
        # Vertex: "Thinking level is unsupported: THINKING_LEVEL_MINIMAL").
        config = agent.AGENTS["interno"].generate_content_config.thinking_config
        self.assertEqual("LOW", getattr(config.thinking_level, "value", config.thinking_level))
        self.assertIsNone(config.thinking_budget)

    def test_region_global_para_gemini(self):
        self.assertEqual("global", agent._VERTEX_LOCATION)
        for fichero in ("deploy.sh", "Dockerfile"):
            with open(os.path.join(os.path.dirname(__file__), "..", fichero)) as f:
                self.assertIn("GOOGLE_CLOUD_LOCATION=global", f.read().replace("$GEMINI_LOCATION", "global"), fichero)


class AlmacenEnLaUETest(unittest.TestCase):

    def test_el_dataset_de_conversaciones_no_hereda_la_region_de_gemini(self):
        import conversation_store
        import inspect
        codigo = inspect.getsource(conversation_store)
        self.assertNotIn('dataset.location = os.environ.get("GOOGLE_CLOUD_LOCATION"', codigo)
        self.assertIn('CONVERSATIONS_LOCATION', codigo)


if __name__ == "__main__":
    unittest.main()
