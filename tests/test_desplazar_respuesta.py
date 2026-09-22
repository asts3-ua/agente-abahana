"""Al llegar una respuesta, la vista va a su principio, no a su final; y solo
entonces: al pulsar un botón la página no debe saltar."""

import inspect
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app


class DesplazarTest(unittest.TestCase):

    def test_una_respuesta_nueva_marca_el_desplazamiento(self):
        codigo = inspect.getsource(chat_app._process_user_prompt)
        self.assertIn('st.session_state["ir_al_inicio_de_la_respuesta"] = True', codigo)

    def test_se_hace_una_sola_vez(self):
        codigo = inspect.getsource(chat_app.main)
        self.assertIn('st.session_state.pop("ir_al_inicio_de_la_respuesta", False)', codigo)

    def test_lleva_al_inicio_del_ultimo_mensaje(self):
        script = chat_app._SCRIPT_IR_AL_INICIO
        self.assertIn('[data-testid="stChatMessage"]', script)
        self.assertIn("stAppScrollToBottomContainer", script)

    def test_el_bloqueo_se_suelta_al_interactuar(self):
        # Si no, la siguiente pregunta ya no se vería abajo.
        script = chat_app._SCRIPT_IR_AL_INICIO
        for evento in ("wheel", "touchstart", "keydown", "pointerdown"):
            self.assertIn(f"'{evento}'", script)
        self.assertIn("delete caja.scrollTop", script)


if __name__ == "__main__":
    unittest.main()
