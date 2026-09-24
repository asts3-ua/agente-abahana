"""Al pinchar una villa del mapa, su panel: resumen, ficha y su calendario.

El calendario que venía en la respuesta es el de la villa que se preguntó y no
cambia al pinchar otra en el mapa; desde el panel se pide el de esa villa, con
aviso mientras se carga.
"""

import inspect
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app


class PanelTest(unittest.TestCase):

    def test_el_panel_ofrece_ficha_y_calendario(self):
        codigo = inspect.getsource(chat_app._render_desplegable_villa)
        self.assertIn("Ver ficha completa", codigo)
        self.assertIn("Ver calendario", codigo)

    def test_mientras_carga_lo_dice(self):
        codigo = inspect.getsource(chat_app._render_calendario_villa)
        self.assertIn("st.spinner", codigo)
        self.assertIn("calendario_html", codigo)

    def test_el_calendario_es_de_la_villa_pinchada(self):
        with patch.object(chat_app.agent, "calendario_villa",
                          return_value={"tramos": [{"desde": "2026-10-01", "hasta": "2026-10-02",
                                                    "tipo_ocupacion": "Libre", "noches": 2}],
                                        "resumen": {"ocupacion_pct": 0}}) as consulta:
            datos = chat_app._datos_calendario("ADORA", 90)
        self.assertEqual("ADORA", consulta.call_args[0][0])
        self.assertEqual(1, len(datos["tramos"]))

    def test_si_falla_no_rompe_la_respuesta(self):
        with patch.object(chat_app.agent, "calendario_villa", side_effect=RuntimeError("caído")), \
             self.assertLogs("chat-app", level="WARNING"):
            # Otro nombre: el de arriba ya está en la caché de Streamlit.
            self.assertIsNone(chat_app._datos_calendario("DIVA", 90))

    def test_el_calendario_sigue_a_la_villa_que_se_pincha(self):
        # Abierto para una villa, al pinchar otra se pinta el de esa otra.
        codigo = inspect.getsource(chat_app._render_desplegable_villa)
        self.assertIn("_render_calendario_villa(nombre)", codigo)
        self.assertIn('st.session_state.get("calendario_abierto")', codigo)


if __name__ == "__main__":
    unittest.main()
