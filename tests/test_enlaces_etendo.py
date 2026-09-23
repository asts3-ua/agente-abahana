"""Acceso directo a Etendo desde la respuesta: abrir la villa o la reserva en
la pantalla que corresponde, sin buscarla a mano."""

import inspect
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app
    import enlaces

VILLA = "BFC7BA9C6D9448ADBFB7219AAF09F502"      # CALAMAR
RESERVA = "D93EBCFB44394A03BA5219CC26A05171"    # 2018_4307


class EnlaceVillaTest(unittest.TestCase):

    def test_el_enlace_que_usa_etendo(self):
        url = enlaces.villa(VILLA, base="https://pre-control.abahanavillas.com")
        self.assertTrue(url.startswith("https://pre-control.abahanavillas.com/etendo/#"))
        crudo = unquote(url)
        self.assertIn(f"targetRecordId:__{VILLA}__", crudo)
        self.assertIn(f"windowId:__{enlaces.VENTANA_VILLAS}__", crudo)
        self.assertIn(f"targetTabId:__{enlaces.PESTANA_VILLAS}__", crudo)
        self.assertIn("tabTitle:__Villas__", crudo)
        self.assertIn("st:1", crudo)
        # Las llaves y los corchetes viajan escapados, como en Etendo.
        self.assertIn("%7B", url)
        self.assertIn("%5B", url)
        self.assertNotIn("{", url)

    def test_sin_id_no_hay_enlace(self):
        self.assertIsNone(enlaces.villa(None, base="https://x.y"))
        self.assertIsNone(enlaces.villa(VILLA, base=""))


class EnlaceReservaTest(unittest.TestCase):

    def test_la_reserva_cuelga_de_su_villa(self):
        crudo = unquote(enlaces.reserva(RESERVA, VILLA, base="https://pre-control.abahanavillas.com"))
        self.assertIn(f"targetRecordId:__{VILLA}__", crudo)
        self.assertIn(f"targetRecordId:__{RESERVA}__", crudo)
        self.assertIn(f"windowId:__{enlaces.VENTANA_RESERVAS}__", crudo)
        self.assertIn(f"targetTabId:__{enlaces.PESTANA_RESERVAS}__", crudo)
        self.assertIn("tabTitle:__Reservas__", crudo)
        self.assertIn("st:2", crudo)

    def test_sin_villa_tambien_abre_la_reserva(self):
        crudo = unquote(enlaces.reserva(RESERVA, None, base="https://x.y"))
        self.assertIn(f"targetRecordId:__{RESERVA}__", crudo)
        self.assertIn("st:1", crudo)


class EnLasHerramientasTest(unittest.TestCase):

    def setUp(self):
        p = patch.object(enlaces, "base_etendo", return_value="https://pre-control.abahanavillas.com")
        p.start()
        self.addCleanup(p.stop)

    def test_la_ficha_de_la_villa_lo_trae(self):
        bq = Mock()
        bq.query.side_effect = [
            Mock(**{"result.return_value": [{"villa_id": VILLA, "nombre": "CALAMAR"}]}),
            Mock(**{"result.return_value": []}),
        ]
        with patch.object(agent, "_bq", bq), \
             patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones"):
            r = agent.obtener_detalle_propiedad("CALAMAR")
        self.assertIn(VILLA, unquote(r["matches"][0]["enlace_etendo"]))
        # El id interno sigue sin salir en la respuesta.
        self.assertNotIn("villa_id", r["matches"][0])

    def test_el_detalle_de_la_reserva_lo_trae(self):
        fila = {"reserva_id": RESERVA, "villa_id": VILLA, "localizador": "2018_4307"}
        bq = Mock()
        bq.query.return_value = Mock(**{"result.return_value": [fila]})
        with patch.object(agent, "_bq", bq), patch.object(agent, "_row_to_dict", side_effect=lambda f: dict(f)):
            r = agent.detalle_reserva("2018_4307")
        self.assertIn(RESERVA, unquote(r["reserva"]["enlace_etendo"]))
        self.assertNotIn("reserva_id", r["reserva"])

    def test_sin_configurar_la_url_no_falla(self):
        with patch.object(enlaces, "base_etendo", return_value=""):
            self.assertIsNone(enlaces.villa(VILLA))

    def test_las_instrucciones_lo_ofrecen(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split())
            self.assertIn("enlace_etendo", texto, rol)
        self.assertNotIn("enlace_etendo", agent.AGENTS["cliente"].instruction)


class EnLaAppTest(unittest.TestCase):

    def test_la_ficha_tiene_boton_de_etendo_solo_para_el_personal(self):
        codigo = inspect.getsource(chat_app._dialogo_ficha)
        self.assertIn("enlace_etendo", codigo)
        self.assertIn("Abrir en Etendo", codigo)

    def test_la_imagen_incluye_el_modulo(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile")) as f:
            self.assertIn("COPY enlaces.py .", f.read())


if __name__ == "__main__":
    unittest.main()
