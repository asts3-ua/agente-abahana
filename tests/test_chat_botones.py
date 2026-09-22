"""Botones de la interfaz: los mensajes no se borran uno a uno; las
conversaciones sí se pueden borrar y renombrar desde la barra lateral."""

import inspect
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app


class BotonesTest(unittest.TestCase):

    def test_los_mensajes_no_tienen_papelera(self):
        codigo = inspect.getsource(chat_app)
        self.assertNotIn("borrarconsulta_", codigo)
        self.assertFalse(hasattr(chat_app, "_render_borrar_consulta"))

    def test_las_conversaciones_si(self):
        self.assertTrue(hasattr(chat_app, "_aviso_borrar_conversacion"))
        self.assertTrue(hasattr(chat_app, "_renombrar_conversacion"))


class CopiarYExcelTest(unittest.TestCase):

    def test_copiar_va_directo_sin_panel(self):
        codigo = inspect.getsource(chat_app._render_exportar)
        self.assertIn("botones_html", codigo)
        self.assertNotIn("popover", codigo)

    def test_valoracion_y_botones_en_la_misma_fila(self):
        self.assertIn("_render_pie_de_respuesta", inspect.getsource(chat_app._render_chat_history))
        pie = inspect.getsource(chat_app._render_pie_de_respuesta)
        self.assertIn("st.columns", pie)
        self.assertLess(pie.index("_render_assistant_feedback"), pie.rindex("_render_exportar"))


if __name__ == "__main__":
    unittest.main()
