"""La respuesta se ve mientras se hace, en vez de un "Consultando..." mudo.

El agente tarda entre 13 y 21 s: las herramientas contestan por el camino (a
los 3-5 s) y el texto se genera al final. Enseñar en qué está y escribir el
texto según llega no acelera nada, pero quita la sensación de cuelgue.
"""

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app


def _evento(*, llamadas=(), respuestas=(), texto=None, parcial=False):
    partes = [SimpleNamespace(text=texto)] if texto is not None else []
    return SimpleNamespace(
        get_function_calls=lambda: [SimpleNamespace(id=f"id-{n}", name=n, args=a) for n, a in llamadas],
        get_function_responses=lambda: [SimpleNamespace(id=f"id-{n}", name=n, response=r) for n, r in respuestas],
        content=SimpleNamespace(parts=partes) if partes else None,
        partial=parcial,
    )


class TrocearEventosTest(unittest.TestCase):

    def test_una_llamada_dice_en_que_esta(self):
        args = {}
        self.assertEqual([("herramienta", "buscar_propiedades")],
                         chat_app._trozos_del_evento(_evento(llamadas=[("buscar_propiedades", {"zona": "Altea"})]), args))
        self.assertEqual({"id-buscar_propiedades": {"zona": "Altea"}}, args)

    def test_la_respuesta_de_la_herramienta_trae_sus_argumentos(self):
        args = {}
        chat_app._trozos_del_evento(_evento(llamadas=[("buscar_propiedades", {"zona": "Altea"})]), args)
        trozos = chat_app._trozos_del_evento(
            _evento(respuestas=[("buscar_propiedades", {"matches": []})]), args)
        self.assertEqual([("resultado", ("buscar_propiedades", {"zona": "Altea"}, {"matches": []}))], trozos)

    def test_el_texto_llega_a_cachos_y_al_final_entero(self):
        self.assertEqual([("texto", "Hemos encontrado ")],
                         chat_app._trozos_del_evento(_evento(texto="Hemos encontrado ", parcial=True), {}))
        self.assertEqual([("final", "Hemos encontrado 2 villas.")],
                         chat_app._trozos_del_evento(_evento(texto="Hemos encontrado 2 villas."), {}))

    def test_un_evento_vacio_no_da_nada(self):
        self.assertEqual([], chat_app._trozos_del_evento(_evento(), {}))


class EtiquetasTest(unittest.TestCase):

    def test_cada_herramienta_se_dice_en_cristiano(self):
        for rol, ag in agent.AGENTS.items():
            for herramienta in ag.tools:
                nombre = getattr(herramienta, "__name__", None)
                if nombre:
                    self.assertIn(nombre, chat_app._ETIQUETA_HERRAMIENTA, f"{rol}: {nombre}")

    def test_una_herramienta_desconocida_no_rompe(self):
        self.assertTrue(chat_app._etiqueta_herramienta("lo_que_sea"))


class EnLaAppTest(unittest.TestCase):

    def test_la_respuesta_se_escribe_segun_llega(self):
        codigo = inspect.getsource(chat_app._process_user_prompt)
        self.assertIn("_stream_agent", codigo)
        self.assertIn("st.status", codigo)

    def test_se_pide_en_modo_streaming(self):
        codigo = inspect.getsource(chat_app._stream_agent)
        self.assertIn("StreamingMode.SSE", codigo)

    def test_si_falla_se_dice_igual_que_antes(self):
        codigo = inspect.getsource(chat_app._process_user_prompt)
        self.assertIn("_mensaje_error(exc)", codigo)


if __name__ == "__main__":
    unittest.main()
