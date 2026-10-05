"""El agente dibuja lo que le pidan.

La imagen pesa un par de megas: no puede pasar por el modelo. La herramienta la
guarda y devuelve solo su identificador; la aplicación la saca del resultado de
la herramienta y la pinta, como el mapa y los gráficos.
"""

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app

PNG = b"\x89PNG" + b"0" * 2000


def _respuesta(datos=PNG, texto="Aquí tienes la imagen"):
    partes = []
    if texto:
        partes.append(SimpleNamespace(text=texto, inline_data=None))
    if datos:
        partes.append(SimpleNamespace(
            text=None, inline_data=SimpleNamespace(data=datos, mime_type="image/png")))
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=partes))])


class GenerarImagenTest(unittest.TestCase):

    def setUp(self):
        agent._imagenes_generadas.clear()

    def _generar(self, respuesta=None, **kw):
        cliente = Mock()
        cliente.models.generate_content.return_value = respuesta or _respuesta()
        with patch.object(agent, "_cliente_imagenes", return_value=cliente):
            self.cliente = cliente
            return agent.generar_imagen(kw.pop("descripcion", "un gato naranja leyendo"), **kw)

    def test_devuelve_un_identificador_y_no_la_imagen(self):
        r = self._generar()
        self.assertIn("imagen_id", r)
        self.assertNotIn("datos", str(r))
        # Lo que ve el modelo tiene que ser corto: la imagen pesa megas.
        self.assertLess(len(str(r)), 500)

    def test_la_imagen_queda_guardada_para_la_aplicacion(self):
        r = self._generar()
        self.assertEqual(PNG, agent.imagen_generada(r["imagen_id"])["datos"])

    def test_el_formato_se_le_pide_al_modelo(self):
        self._generar(formato="cuadrado")
        config = self.cliente.models.generate_content.call_args.kwargs["config"]
        self.assertEqual("1:1", config.image_config.aspect_ratio)

    def test_formato_por_defecto_horizontal(self):
        self._generar()
        config = self.cliente.models.generate_content.call_args.kwargs["config"]
        self.assertEqual("16:9", config.image_config.aspect_ratio)

    def test_un_formato_raro_no_rompe(self):
        r = self._generar(formato="panorámica gigante")
        self.assertIn("imagen_id", r)

    def test_si_el_modelo_no_dibuja_lo_dice(self):
        r = self._generar(respuesta=_respuesta(datos=None, texto="No puedo dibujar eso"))
        self.assertIn("error", r)
        self.assertNotIn("imagen_id", r)

    def test_si_falla_la_llamada_lo_dice(self):
        cliente = Mock()
        cliente.models.generate_content.side_effect = RuntimeError("se cayó")
        with patch.object(agent, "_cliente_imagenes", return_value=cliente), \
             self.assertLogs("agente-villas", level="ERROR"):
            r = agent.generar_imagen("algo")
        self.assertIn("error", r)

    def test_no_se_guardan_mil_imagenes(self):
        for i in range(agent._MAX_IMAGENES + 3):
            self._generar(descripcion=f"dibujo {i}")
        self.assertLessEqual(len(agent._imagenes_generadas), agent._MAX_IMAGENES)

    def test_la_tienen_los_tres_agentes(self):
        for rol, ag in agent.AGENTS.items():
            self.assertIn("generar_imagen", {t.__name__ for t in ag.tools}, rol)


class AsistenteGeneralistaTest(unittest.TestCase):

    def test_responde_de_todo_no_solo_de_villas(self):
        for rol, ag in agent.AGENTS.items():
            texto = " ".join(ag.instruction.split()).lower()
            self.assertIn("cualquier tema", texto, rol)
            self.assertIn("generar_imagen", texto, rol)

    def test_sigue_sin_inventarse_los_datos_de_abahana(self):
        for rol, ag in agent.AGENTS.items():
            self.assertIn("Nunca inventes datos", ag.instruction, rol)


class EnLaAppTest(unittest.TestCase):

    def test_las_imagenes_del_turno_se_recogen(self):
        herramientas = [("generar_imagen", {"descripcion": "un gato"},
                         {"imagen_id": "abc", "descripcion": "un gato"})]
        with patch.object(chat_app.agent, "imagen_generada",
                          return_value={"datos": PNG, "descripcion": "un gato"}):
            imagenes = chat_app._imagenes_del_turno(herramientas)
        self.assertEqual([{"datos": PNG, "descripcion": "un gato"}], imagenes)

    def test_una_imagen_que_ya_no_esta_no_rompe(self):
        with patch.object(chat_app.agent, "imagen_generada", return_value=None):
            self.assertEqual([], chat_app._imagenes_del_turno(
                [("generar_imagen", {}, {"imagen_id": "perdida"})]))

    def test_se_pintan_en_la_respuesta(self):
        codigo = inspect.getsource(chat_app._render_chat_history)
        self.assertIn("_render_imagenes(msg)", codigo)
        pintar = inspect.getsource(chat_app._render_imagenes)
        self.assertIn("st.image", pintar)
        self.assertIn("download_button", pintar)   # para guardarla

    def test_el_turno_las_guarda(self):
        self.assertIn('"imagenes": _imagenes_del_turno(herramientas)',
                      inspect.getsource(chat_app._process_user_prompt))


if __name__ == "__main__":
    unittest.main()
