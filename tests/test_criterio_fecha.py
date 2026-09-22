""""Reservas confirmadas este mes" puede ser confirmadas durante el mes o con
entrada en el mes: en septiembre de 2026 eran 86 frente a 463, y solo 15
cumplían las dos. El agente tiene que poder usar cada criterio y decir cuál usa.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import filtros


def _consulta(funcion, **kwargs):
    bq = Mock()
    bq.query.return_value.result.return_value = []
    with patch.object(agent, "_bq", bq), patch.object(agent, "_nombres_villas", return_value=[]):
        resultado = funcion(**kwargs)
    llamada = bq.query.call_args_list[0]
    params = {p.name: p.value for p in llamada.kwargs["job_config"].query_parameters}
    return llamada[0][0], params, resultado


class ConsultarReservasTest(unittest.TestCase):

    def test_confirmadas_durante_un_periodo(self):
        sql, params, _ = _consulta(agent.consultar_reservas,
                                   confirmada_desde="2026-09-01", confirmada_hasta="2026-09-30")
        self.assertIn("r.fecha_confirmacion >= @confirmada_desde", sql)
        self.assertIn("r.fecha_confirmacion <= @confirmada_hasta", sql)
        self.assertNotIn("r.fecha_entrada >=", sql)

    def test_hechas_durante_un_periodo(self):
        sql, _, _ = _consulta(agent.consultar_reservas, creada_desde="2026-09-01", creada_hasta="2026-09-30")
        self.assertIn("r.fecha_pedido >= @creada_desde", sql)

    def test_cada_reserva_trae_sus_fechas_de_confirmacion_y_creacion(self):
        sql, _, _ = _consulta(agent.consultar_reservas)
        self.assertIn("r.fecha_confirmacion", sql)
        self.assertIn("r.fecha_pedido", sql)

    def test_dice_que_criterio_de_fecha_ha_usado(self):
        _, _, r = _consulta(agent.consultar_reservas, confirmada_desde="2026-09-01")
        self.assertEqual(["fecha de confirmación"], r["criterio_fecha"])
        _, _, r = _consulta(agent.consultar_reservas, fecha_desde="2026-09-01")
        self.assertEqual(["fecha de entrada"], r["criterio_fecha"])


class ResumenReservasTest(unittest.TestCase):

    def test_por_defecto_manda_la_entrada(self):
        sql, _, r = _consulta(agent.resumen_reservas, agrupar_por="mes", fecha_desde="2026-01-01")
        self.assertIn("FORMAT_DATE('%Y-%m', r.fecha_entrada)", sql)
        self.assertEqual("fecha de entrada", r["criterio_fecha"])

    def test_por_confirmacion_agrupa_y_filtra_por_esa_fecha(self):
        sql, _, r = _consulta(agent.resumen_reservas, agrupar_por="mes", criterio_fecha="confirmacion",
                              fecha_desde="2026-09-01", fecha_hasta="2026-09-30")
        self.assertIn("FORMAT_DATE('%Y-%m', r.fecha_confirmacion)", sql)
        self.assertIn("r.fecha_confirmacion >= @fecha_desde", sql)
        self.assertIn("r.fecha_confirmacion IS NOT NULL", sql)
        self.assertEqual("fecha de confirmación", r["criterio_fecha"])

    def test_por_creacion(self):
        sql, _, _ = _consulta(agent.resumen_reservas, criterio_fecha="creacion", fecha_desde="2026-09-01")
        self.assertIn("r.fecha_pedido >= @fecha_desde", sql)


class RecuadroDeFiltrosTest(unittest.TestCase):

    def test_se_ve_el_criterio(self):
        self.assertIn("confirmadas del mar 1 al mié 30 sep 2026", filtros.describir_llamada(
            "consultar_reservas", {"confirmada_desde": "2026-09-01", "confirmada_hasta": "2026-09-30"}))
        self.assertIn("hechas desde el mar 1 sep 2026", filtros.describir_llamada(
            "consultar_reservas", {"creada_desde": "2026-09-01"}))
        resumen = filtros.describir_llamada(
            "resumen_reservas", {"criterio_fecha": "confirmacion", "fecha_desde": "2026-09-01",
                                 "fecha_hasta": "2026-09-30", "agrupar_por": "mes"})
        self.assertIn("confirmadas del mar 1 al mié 30 sep 2026", resumen)
        self.assertNotIn("entrada", resumen)
        self.assertNotIn("criterio_fecha", resumen)
        self.assertIn("por fecha de confirmación", filtros.describir_llamada(
            "resumen_reservas", {"criterio_fecha": "confirmacion", "agrupar_por": "mes"}))

    def test_los_parametros_nuevos_no_salen_en_crudo(self):
        linea = filtros.describir_llamada("consultar_reservas", {
            "localizador": "2026_3274", "titular": "Olivia Farly", "ordenar_por": "importe",
            "confirmada_desde": "2026-09-01"})
        for crudo in ("localizador:", "titular:", "ordenar_por", "confirmada_desde"):
            self.assertNotIn(crudo, linea)
        self.assertIn("nº 2026_3274", linea)
        self.assertIn("titular Olivia Farly", linea)
        self.assertIn("más caras primero", linea)


class InstruccionesTest(unittest.TestCase):

    def test_ante_la_duda_pregunta_antes_de_consultar(self):
        # El usuario prefiere que pregunte a recibir todas las lecturas.
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split()).lower()
            self.assertIn("pregunta primero qué fecha quiere", texto, rol)
            self.assertNotIn("da las dos cifras", texto, rol)

    def test_si_la_pregunta_es_clara_no_pregunta_y_dice_el_criterio(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split()).lower()
            self.assertIn("si la pregunta es clara", texto, rol)
            self.assertIn("di siempre qué fecha has usado", texto, rol)


if __name__ == "__main__":
    unittest.main()
