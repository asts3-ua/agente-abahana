"""El precio que se da es el que paga el cliente por noche, no solo la tarifa
de la villa.

En DIVA (nov 2026) la línea PrecioVilla son 88 €/noche, pero la energía son
otros 70 € obligatorios: el cliente paga 166,31 €, que es lo que guarda la
ocupación en `precio_tarifa_sin_fees` y lo que cobra Etendo.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class TarifaDeLaEstanciaTest(unittest.TestCase):

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        p = patch.object(agent, "_bq", self.bq)
        p.start()
        self.addCleanup(p.stop)
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def _sql(self, n=0):
        return self.bq.query.call_args_list[n][0][0]

    def test_las_ofertas_cobran_la_noche_entera(self):
        agent.buscar_ofertas(fecha_desde="2099-10-01", fecha_hasta="2099-10-08")
        sql = self._sql()
        self.assertIn("precio_tarifa_sin_fees", sql)

    def test_la_compra_incluye_los_extras(self):
        # Si la venta lleva la energía y la compra no, el margen sale inflado.
        agent.buscar_ofertas(fecha_desde="2099-10-01", fecha_hasta="2099-10-08")
        self.assertIn("compra_std", self._sql())
        self.assertNotIn(f"AND t.nombre = '{agent._CONCEPTO_VILLA}'\n      AND t.es_venta IS NOT NULL",
                         self._sql())

    def test_las_fechas_alternativas_tambien(self):
        import inspect
        self.assertIn("precio_tarifa_sin_fees", inspect.getsource(agent.alternativas_villa))

    def test_el_detalle_de_precios_separa_las_dos_cifras(self):
        agent.consultar_precios("ADORA", "2099-10-01", "2099-10-07")
        self.assertIn("precio_tarifa_sin_fees", self._sql())


class ComponerPreciosTest(unittest.TestCase):

    FILAS = [
        {"fecha": "2026-11-14", "estado": "Libre", "nombre": "PrecioVilla", "tipo": "std",
         "es_venta": True, "precio_final": 88.0, "precio_cliente": 166.31},
        {"fecha": "2026-11-14", "estado": "Libre", "nombre": "PrecioVilla", "tipo": "std",
         "es_venta": False, "precio_final": 52.0, "precio_cliente": 166.31},
        {"fecha": "2026-11-14", "estado": "Libre", "nombre": "Energía", "tipo": "std",
         "es_venta": True, "precio_final": 70.0, "precio_cliente": 166.31},
        {"fecha": "2026-11-14", "estado": "Libre", "nombre": "Energía", "tipo": "std",
         "es_venta": False, "precio_final": 42.0, "precio_cliente": 166.31},
    ]

    def test_la_noche_trae_el_precio_del_cliente_y_el_de_la_villa(self):
        import datetime
        r = agent._componer_precios(self.FILAS, datetime.date(2026, 11, 14), datetime.date(2026, 11, 14))
        noche = r["noches"][0]
        self.assertEqual(166.31, noche["precio_cliente"])
        self.assertEqual(88.0, noche["precio_venta"])
        self.assertEqual(52.0, noche["precio_compra"])
        self.assertEqual(166.31, r["resumen"]["total_cliente"])

    def test_el_aviso_explica_la_diferencia(self):
        import datetime
        r = agent._componer_precios(self.FILAS, datetime.date(2026, 11, 14), datetime.date(2026, 11, 14))
        self.assertIn("extras", r["resumen"]["nota"].lower())


class InstruccionesTest(unittest.TestCase):

    def test_dicen_cual_es_el_precio_del_cliente(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split())
            self.assertIn("precio_cliente", texto, rol)


if __name__ == "__main__":
    unittest.main()
