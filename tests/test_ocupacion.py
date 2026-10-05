"""Qué cuenta como ocupación, y que el agente lo entienda igual que el usuario.

Decisión del negocio (2026-10-05): las noches que el propietario usa su propia
villa salen de la fórmula igual que las bloqueadas. Esos días la casa no
estaba a la venta, así que ni se vendieron ni se dejaron de vender.

No es un matiz: en junio de 2026 hay 2.330 noches de propietario frente a
2.402 comerciales. Según cómo se traten, la ocupación del mes sale 77 %, 66 %
o 45 %.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class _Fila(dict):
    """Imita una Row de BigQuery: se comporta como dict con .items()."""


def _noches(**por_tipo):
    """Un día por cada noche, como los devuelve Ocupacion."""
    filas, dia = [], 1
    for tipo, cuantas in por_tipo.items():
        tipo = tipo.replace("_", " ").title()
        for _ in range(cuantas):
            filas.append({"fecha": f"2026-06-{dia:02d}", "tipo_ocupacion": tipo,
                          "reserva_id": None, "villa_id": "V1",
                          "villa_nombre": "ADORA"})
            dia += 1
    return filas


class FormulaTest(unittest.TestCase):

    def _resumen(self, **por_tipo):
        import datetime
        return agent._componer_calendario(
            _noches(**por_tipo),
            datetime.date(2026, 6, 1), datetime.date(2026, 6, 30))["resumen"]

    def test_las_del_propietario_no_cuentan_como_vendidas(self):
        r = self._resumen(reserva=6, reserva_propietario=4, libre=2)
        self.assertEqual(6, r["noches_vendidas"])
        self.assertEqual(4, r["noches_propietario"])

    def test_tampoco_cuentan_como_comercializables(self):
        r = self._resumen(reserva=6, reserva_propietario=4, libre=2)
        # 6 vendidas + 2 libres: las 4 del propietario no estaban a la venta.
        self.assertEqual(8, r["noches_comercializables"])
        self.assertEqual(75.0, r["ocupacion_pct"])

    def test_las_bloqueadas_siguen_fuera(self):
        r = self._resumen(reserva=3, libre=2, no_disponible=5)
        self.assertEqual(5, r["noches_comercializables"])
        self.assertEqual(60.0, r["ocupacion_pct"])

    def test_la_agencia_si_es_venta(self):
        r = self._resumen(reserva=2, reserva_agencia=2, libre=4)
        self.assertEqual(4, r["noches_vendidas"])
        self.assertEqual(50.0, r["ocupacion_pct"])

    def test_un_mes_entero_de_propietario_no_es_ocupacion_del_cien(self):
        # Antes salía 100 %: no se vendió nada, no hay nada que medir.
        r = self._resumen(reserva_propietario=10)
        self.assertEqual(0, r["noches_comercializables"])
        self.assertIsNone(r["ocupacion_pct"])

    def test_el_canal_no_mezcla_al_propietario_con_las_ventas(self):
        r = self._resumen(reserva=2, reserva_agencia=1, reserva_propietario=3)
        self.assertEqual({"Reserva": 2, "Reserva Agencia": 1}, r["por_canal"])


class ResumenOcupacionTest(unittest.TestCase):
    """La pregunta real: "ocupación de las villas en junio", muchas villas."""

    def setUp(self):
        self.bq = Mock()
        p = patch.object(agent, "_bq", self.bq)
        p.start()
        self.addCleanup(p.stop)

    def _responde(self, filas):
        self.bq.query.return_value = Mock(**{"result.return_value": [_Fila(f) for f in filas]})

    def _sql(self):
        return self.bq.query.call_args[0][0]

    def test_calcula_el_porcentaje_con_el_criterio_acordado(self):
        self._responde([{"dimension": "ADORA", "noches_vendidas": 18,
                         "noches_libres": 6, "noches_bloqueadas": 3,
                         "noches_propietario": 3}])
        r = agent.resumen_ocupacion(fecha_desde="2026-06-01", fecha_hasta="2026-06-30")
        fila = r["resumen"][0]
        self.assertEqual(24, fila["noches_comercializables"])
        self.assertEqual(75.0, fila["ocupacion_pct"])

    def test_trae_totales_del_conjunto(self):
        self._responde([{"dimension": "ADORA", "noches_vendidas": 9, "noches_libres": 1,
                         "noches_bloqueadas": 0, "noches_propietario": 0},
                        {"dimension": "BELLA", "noches_vendidas": 1, "noches_libres": 9,
                         "noches_bloqueadas": 0, "noches_propietario": 0}])
        r = agent.resumen_ocupacion(fecha_desde="2026-06-01", fecha_hasta="2026-06-30")
        # 10 vendidas sobre 20 comercializables: la media del conjunto, no la
        # media de los dos porcentajes (90 % y 10 %).
        self.assertEqual(50.0, r["totales"]["ocupacion_pct"])

    def test_el_sql_excluye_al_propietario_del_recuento_de_vendidas(self):
        self._responde([])
        agent.resumen_ocupacion(fecha_desde="2026-06-01", fecha_hasta="2026-06-30")
        self.assertIn("Reserva Propietario", self._sql())

    def test_no_cuenta_las_villas_de_prueba(self):
        self._responde([])
        agent.resumen_ocupacion(fecha_desde="2026-06-01", fecha_hasta="2026-06-30")
        self.assertIn("villa_dedup", self._sql())

    def test_agrupa_por_mes_zona_o_villa(self):
        for agrupar, esperado in (("mes", "FORMAT_DATE"), ("zona", "v.zona"),
                                  ("villa", "v.nombre")):
            self._responde([])
            agent.resumen_ocupacion(agrupar_por=agrupar, fecha_desde="2026-06-01",
                                    fecha_hasta="2026-06-30")
            self.assertIn(esperado, self._sql(), agrupar)

    def test_exige_el_periodo(self):
        r = agent.resumen_ocupacion(fecha_desde="", fecha_hasta="")
        self.assertIn("error", r)

    def test_un_fallo_de_bigquery_no_revienta(self):
        self.bq.query.side_effect = RuntimeError("se cayó")
        r = agent.resumen_ocupacion(fecha_desde="2026-06-01", fecha_hasta="2026-06-30")
        self.assertIn("error", r)


class ElAgenteLoSabeTest(unittest.TestCase):

    def test_las_instrucciones_fijan_el_criterio(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split()).lower()
            self.assertIn("ocupación", texto, rol)
            self.assertIn("propietario", texto, rol)

    def test_la_herramienta_esta_disponible_para_el_personal(self):
        for rol in ("interno", "admin"):
            nombres = {t.__name__ for t in agent.AGENTS[rol].tools}
            self.assertIn("resumen_ocupacion", nombres, rol)

    def test_el_cliente_no_ve_la_ocupacion_interna(self):
        nombres = {t.__name__ for t in agent.AGENTS["cliente"].tools}
        self.assertNotIn("resumen_ocupacion", nombres)


class GraficoTest(unittest.TestCase):
    """El modelo anuncia "el gráfico a continuación": tiene que estar."""

    def _recoge(self, respuesta):
        import visualizaciones
        return visualizaciones.recoger([("resumen_ocupacion",
                                         {"agrupar_por": "zona"}, respuesta)])

    def test_la_ocupacion_da_un_grafico(self):
        v = self._recoge({"agrupar_por": "zona", "resumen": [
            {"dimension": "Altea", "ocupacion_pct": 77.4, "noches_vendidas": 24,
             "noches_comercializables": 31}], "totales": {"ocupacion_pct": 77.4}})
        self.assertEqual(["ocupacion"], [x["tipo"] for x in v])

    def test_se_puede_pintar(self):
        import visualizaciones
        visualizaciones.grafico_ocupacion({"agrupar_por": "zona", "filas": [
            {"dimension": "Altea", "ocupacion_pct": 77.4, "noches_vendidas": 24,
             "noches_comercializables": 31, "noches_propietario": 2}]})

    def test_una_villa_sin_nada_que_vender_no_pinta_un_cero(self):
        import visualizaciones
        grafico = visualizaciones.grafico_ocupacion({"agrupar_por": "villa", "filas": [
            {"dimension": "ADORA", "ocupacion_pct": None, "noches_vendidas": 0,
             "noches_comercializables": 0}]})
        self.assertTrue(grafico.to_dict())

    def test_una_respuesta_con_error_no_pinta(self):
        self.assertEqual([], self._recoge({"error": "se cayó", "resumen": []}))


if __name__ == "__main__":
    unittest.main()
