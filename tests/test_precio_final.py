"""Precio final de una estancia, preguntándoselo a Etendo en vivo.

Las tarifas del lake son solo la villa: la web cobra además los extras
obligatorios (limpieza final...). Este precio lo calcula Etendo, y el agente
suma las líneas obligatorias y explica el desglose con los nombres de los
productos de BigQuery.
"""

import datetime
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import frescura

RESPUESTA = {"response": {"status": "0", "data": [{
    "resource_id": "02487AAA94DE4667A8CFE99ED7FAE9C8",
    "villa_id": "4751C72903A94E13967D6628DD42FBEA",
    "fecha_inicio": "2027-07-03", "fecha_fin": "2027-07-10",
    "precio_antes": 3838.8322, "precio_despues": 3838.8322,
    "lineas": [
        {"id_producto": "4751C72903A94E13967D6628DD42FBEA", "precio_antes": 3838.8322,
         "precio_despues": 3838.8322, "obligatorio": True, "tipo": "1", "repetible": False, "calculo": "d"},
        {"id_producto": "211388AD79D34B4D9DBCB2CD68F6A1FA", "precio_antes": 264,
         "precio_despues": 264, "obligatorio": True, "tipo": "2", "repetible": False, "calculo": "r"},
        {"id_producto": "65773CDB62144A84B9DAB725FB93BB29", "precio_antes": 0,
         "precio_despues": 0, "obligatorio": True, "tipo": "2", "repetible": False, "calculo": "d"},
        {"id_producto": "618E566BD35C4A7EB7CAB135D0201780", "precio_antes": -191.8322,
         "precio_despues": -191.8322, "obligatorio": False, "tipo": "2", "repetible": False, "calculo": "r"},
        {"id_producto": "C16844F671C841799CB56B5CEB94B39A", "precio_antes": 70,
         "precio_despues": 70, "obligatorio": False, "tipo": "2", "repetible": False, "calculo": "r"},
        {"id_producto": "024CA4EFF9A746838409C9206C3E538B", "precio_antes": 4,
         "precio_despues": 4, "obligatorio": False, "tipo": "2", "repetible": True, "calculo": "d"},
    ],
}]}}

PRODUCTOS = {
    "4751C72903A94E13967D6628DD42FBEA": "ANA CLARA",
    "211388AD79D34B4D9DBCB2CD68F6A1FA": "Limpieza final",
    "65773CDB62144A84B9DAB725FB93BB29": "Energía",
    "618E566BD35C4A7EB7CAB135D0201780": "Descuento pago completo",
    "C16844F671C841799CB56B5CEB94B39A": "Animales",
    "024CA4EFF9A746838409C9206C3E538B": "Cuna Bebé",
}


def _con_etendo(respuesta=RESPUESTA, estado=200, villa=("02487AAA94DE4667A8CFE99ED7FAE9C8", "ANA CLARA")):
    get = patch.object(agent.requests, "get",
                       return_value=Mock(status_code=estado, json=Mock(return_value=respuesta),
                                         text=str(respuesta)))
    return (get,
            patch.object(agent, "_villa_por_nombre", return_value=villa),
            patch.object(agent, "_productos_etendo", return_value=PRODUCTOS),
            patch.object(agent, "_credenciales_etendo",
                         return_value=("u", "p", "https://pre-control.abahanavillas.com")))


class PrecioFinalTest(unittest.TestCase):

    def _llamar(self, **kw):
        parches = _con_etendo(**kw)
        with parches[0] as get, parches[1], parches[2], parches[3]:
            self.get = get
            return agent.precio_final_villa("ANA CLARA", "2027-07-03", "2027-07-10")

    def test_pregunta_a_etendo_por_el_id_de_la_villa(self):
        self._llamar()
        url = self.get.call_args[0][0]
        params = self.get.call_args.kwargs["params"]
        self.assertIn("es.opentix.webservices.preciovilla", url)
        self.assertEqual("02487AAA94DE4667A8CFE99ED7FAE9C8", params["resourceId"])
        self.assertEqual("2027-07-03", params["dateFrom"])
        self.assertEqual("2027-07-10", params["dateTo"])
        self.assertEqual("si", params["extras"])
        self.assertEqual(("u", "p"), self.get.call_args.kwargs["auth"])

    def test_suma_solo_las_lineas_obligatorias(self):
        r = self._llamar()
        self.assertEqual(4102.83, r["precio_final"])
        self.assertEqual(3838.83, r["alojamiento"])
        self.assertEqual(7, r["noches"])
        self.assertEqual(586.12, r["precio_medio_noche"])

    def test_el_desglose_lleva_el_nombre_de_cada_producto(self):
        r = self._llamar()
        self.assertEqual([{"concepto": "Limpieza final", "importe": 264.0,
                           "cobro": "por reserva"}], r["obligatorios"])
        self.assertIn("Energía", r["incluidos_sin_coste"])
        self.assertEqual("ANA CLARA", r["villa"])

    def test_los_opcionales_van_aparte_y_no_suman(self):
        r = self._llamar()
        opcionales = {o["concepto"]: o for o in r["opcionales"]}
        self.assertEqual(-191.83, opcionales["Descuento pago completo"]["importe"])
        self.assertEqual(70.0, opcionales["Animales"]["importe"])
        self.assertNotIn("Animales", str(r["obligatorios"]))

    def test_dice_si_se_cobra_por_dia_o_por_reserva(self):
        # En Etendo, calculo "d" se multiplica por las noches y "r" no.
        opcionales = {o["concepto"]: o for o in self._llamar()["opcionales"]}
        self.assertEqual("por reserva", opcionales["Animales"]["cobro"])
        self.assertEqual("por día", opcionales["Cuna Bebé"]["cobro"])
        # Los repetibles se pueden pedir varios: el importe es por unidad.
        self.assertTrue(opcionales["Cuna Bebé"]["por_unidad"])
        self.assertNotIn("por_unidad", opcionales["Animales"])

    def test_las_instrucciones_piden_decirlo(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split())
            self.assertIn("por día", texto, rol)
            self.assertIn("por reserva", texto, rol)

    def test_un_producto_desconocido_no_rompe(self):
        respuesta = {"response": {"status": "0", "data": [{
            "lineas": [{"id_producto": "NUEVO", "precio_despues": 10, "obligatorio": True, "tipo": "2"}],
        }]}}
        r = self._llamar(respuesta=respuesta)
        self.assertEqual(10.0, r["precio_final"])
        self.assertEqual("NUEVO", r["obligatorios"][0]["concepto"])

    def test_si_etendo_falla_lo_dice(self):
        r = self._llamar(estado=500)
        self.assertIn("error", r)
        self.assertNotIn("precio_final", r)

    def test_si_la_villa_no_existe_sugiere(self):
        with patch.object(agent, "_villa_por_nombre", return_value=(None, None)), \
             patch.object(agent, "_sugerir_villas", return_value=["ANA CLARA"]):
            r = agent.precio_final_villa("Ana Klara", "2027-07-03", "2027-07-10")
        self.assertIn("ANA CLARA", r["sugerencias"])

    def test_fechas_mal_puestas(self):
        with patch.object(agent, "_villa_por_nombre", return_value=("X", "ANA CLARA")):
            r = agent.precio_final_villa("ANA CLARA", "2027-07-10", "2027-07-03")
        self.assertIn("error", r)

    def test_la_tienen_interno_y_admin_pero_no_el_cliente(self):
        # La versión cliente no da precios: tampoco este.
        for rol in ("interno", "admin"):
            self.assertIn("precio_final_villa", {t.__name__ for t in agent.AGENTS[rol].tools}, rol)
        self.assertNotIn("precio_final_villa", {t.__name__ for t in agent.AGENTS["cliente"].tools})

    def test_las_instrucciones_distinguen_las_dos_herramientas(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split())
            self.assertIn("precio_final_villa", texto, rol)
            self.assertIn("limpieza final", texto.lower(), rol)


class FrescuraTest(unittest.TestCase):

    def test_es_un_dato_en_vivo_de_etendo(self):
        self.assertTrue(frescura.uso_en_vivo([("precio_final_villa", {}, {})]))
        self.assertFalse(frescura.uso_en_vivo([("consultar_precios", {}, {})]))

    def test_la_linea_lo_dice(self):
        ahora = datetime.datetime(2026, 9, 23, 13, 30, tzinfo=frescura._MADRID)
        self.assertEqual(["Precio final: consultado en Etendo hoy a las 13:30"],
                         frescura.lineas([], {}, ahora, vivo=True))


if __name__ == "__main__":
    unittest.main()
