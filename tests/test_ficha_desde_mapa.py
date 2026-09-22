"""Con un mapa de varias villas, el botón de la ficha va en el desplegable que
se abre al pulsar una villa del mapa, no en una fila de botones aparte."""

import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app
    import visualizaciones as viz

MAPA = {"tipo": "mapa", "aproximado": False, "villas": [
    {"nombre": "ADORA", "lat": 38.66, "lon": 0.06, "pueblo_cercano": "Calpe", "capacidad_pax": 8},
    {"nombre": "ALDABA", "lat": 38.65, "lon": 0.05, "pueblo_cercano": "Calpe"},
]}


class SeleccionEnElMapaTest(unittest.TestCase):

    def test_la_capa_se_puede_seleccionar_por_su_id(self):
        self.assertEqual(viz.CAPA_VILLAS, viz.mapa_deck(MAPA).layers[0].id)

    def test_la_villa_pulsada(self):
        evento = SimpleNamespace(selection=SimpleNamespace(
            objects={viz.CAPA_VILLAS: [{"nombre": "ALDABA", "lat": 38.65}]}, indices={}))
        self.assertEqual("ALDABA", viz.villa_pulsada(evento))

    def test_sin_pulsar_nada(self):
        self.assertIsNone(viz.villa_pulsada(SimpleNamespace(selection=SimpleNamespace(objects={}))))
        self.assertIsNone(viz.villa_pulsada(None))

    def test_el_mapa_avisa_de_que_se_puede_pulsar(self):
        self.assertIn("Pulsa", json.dumps(viz.tooltip_mapa(MAPA), ensure_ascii=False))

    def test_render_devuelve_la_villa_pulsada(self):
        st = Mock()
        st.pydeck_chart.return_value = SimpleNamespace(selection=SimpleNamespace(
            objects={viz.CAPA_VILLAS: [{"nombre": "ADORA"}]}))
        with patch.dict("sys.modules", {"streamlit": st}):
            self.assertEqual("ADORA", viz.render(MAPA, key="mapa_1"))
        kwargs = st.pydeck_chart.call_args.kwargs
        self.assertEqual("rerun", kwargs["on_select"])
        self.assertEqual("single-object", kwargs["selection_mode"])
        self.assertEqual("mapa_1", kwargs["key"])


class BotonesTest(unittest.TestCase):

    def test_las_villas_del_mapa_no_repiten_boton(self):
        msg = {"villas": ["ADORA", "ALDABA", "DIVA"], "visualizaciones": [MAPA]}
        self.assertEqual(["DIVA"], chat_app._villas_sin_mapa(msg))

    def test_con_una_sola_villa_en_el_mapa_sigue_el_boton(self):
        msg = {"villas": ["ADORA"], "visualizaciones": [dict(MAPA, villas=MAPA["villas"][:1])]}
        self.assertEqual(["ADORA"], chat_app._villas_sin_mapa(msg))

    def test_sin_mapa_todas(self):
        self.assertEqual(["ADORA"], chat_app._villas_sin_mapa({"villas": ["ADORA"]}))

    def test_el_desplegable_abre_la_ficha(self):
        codigo = inspect.getsource(chat_app._render_desplegable_villa)
        self.assertIn("Ver ficha completa", codigo)
        self.assertIn("_pedir_ficha", codigo)

    def test_cada_mapa_tiene_su_clave(self):
        self.assertIn("key=", inspect.getsource(chat_app._render_visualizaciones))


if __name__ == "__main__":
    unittest.main()
