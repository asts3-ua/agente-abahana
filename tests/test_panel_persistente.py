"""El panel de la villa aguanta un clic en un hueco del mapa.

Al pinchar fuera de un punto, el mapa devuelve la selección vacía: el panel y
su botón "Ver ficha completa" desaparecían, y parecía que la ficha ya no se
podía abrir.
"""

import inspect
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app


class PanelPersistenteTest(unittest.TestCase):

    def test_se_recuerda_la_ultima_villa_de_cada_mapa(self):
        estado = {}
        with patch.object(chat_app.st, "session_state", estado):
            chat_app._recordar_villa("t1_0", "ADORA")
            chat_app._recordar_villa("t2_0", "DIVA")
            self.assertEqual("ADORA", chat_app._villa_recordada("t1_0"))
            self.assertEqual("DIVA", chat_app._villa_recordada("t2_0"))

    def test_sin_nada_pinchado_no_hay_panel(self):
        with patch.object(chat_app.st, "session_state", {}):
            self.assertIsNone(chat_app._villa_recordada("t1_0"))

    def test_el_mapa_vacio_no_borra_el_panel(self):
        codigo = inspect.getsource(chat_app._render_visualizaciones)
        self.assertIn("or _villa_recordada(clave)", codigo)
        self.assertIn("_recordar_villa(clave, pulsada)", codigo)

    def test_al_pinchar_una_villa_se_asoma_su_panel(self):
        # La barra de escribir tapa lo último de la respuesta.
        codigo = inspect.getsource(chat_app._render_visualizaciones)
        self.assertIn("_ver_panel(clave)", codigo)
        self.assertIn("scrollIntoView", chat_app._SCRIPT_VER_PANEL)

    def test_solo_cuando_cambia_la_villa(self):
        # Si no, la página saltaría en cada repintado.
        codigo = inspect.getsource(chat_app._render_visualizaciones)
        self.assertIn("nueva = _villa_recordada(clave) != pulsada", codigo)
        self.assertIn("if nueva:", codigo)

    def test_al_cerrar_la_ficha_la_pagina_se_repinta(self):
        # Sin esto el servidor no se entera de que se ha cerrado.
        codigo = inspect.getsource(chat_app._dialogo_ficha)
        self.assertIn('on_dismiss="rerun"', codigo)


if __name__ == "__main__":
    unittest.main()
