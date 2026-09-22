"""Regresiones de los fallos críticos: respuestas incorrectas sin aviso."""

import datetime
import re
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class _Fila(dict):
    """Imita una Row de BigQuery: se comporta como dict con .items()."""


def _bq_con(filas):
    bq = Mock()
    bq.query.return_value.result.return_value = filas
    return bq


def _sql(bq) -> str:
    return bq.query.call_args[0][0]


def _params(bq) -> dict:
    return {p.name: p.value for p in
            bq.query.call_args[1]["job_config"].query_parameters}


# ---------------------------------------------------------------------------
# 1. Precios y calendario no pueden mezclar villas
# ---------------------------------------------------------------------------

def _tarifa(villa_id, villa, fecha, es_venta, precio):
    return _Fila(villa_id=villa_id, villa_nombre=villa, fecha=fecha,
                 estado="0", nombre="PrecioVilla", tipo="std",
                 es_venta=es_venta, precio_final=precio, descuento_pct=0.0,
                 estancia_minima_noches=0)


class PreciosDeUnaSolaVillaTest(unittest.TestCase):
    """`LIKE '%ADORA%'` casa con ADORA y con ADORABLE: sus precios del mismo
    día se pisaban y salía el de una villa cualquiera."""

    FILAS = [
        _tarifa("v1", "ADORA", "2027-07-01", True, 400.0),
        _tarifa("v1", "ADORA", "2027-07-01", False, 300.0),
        _tarifa("v2", "ADORABLE", "2027-07-01", True, 900.0),
        _tarifa("v2", "ADORABLE", "2027-07-01", False, 100.0),
    ]

    def test_si_un_nombre_casa_exacto_usa_solo_esa_villa(self):
        with patch.object(agent, "_bq", _bq_con(self.FILAS)):
            r = agent.consultar_precios("adora", "2027-07-01", "2027-07-01")
        self.assertEqual("ADORA", r["villa"])
        self.assertEqual(400.0, r["noches"][0]["precio_venta"])
        self.assertEqual(300.0, r["noches"][0]["precio_compra"])

    def test_si_es_ambiguo_no_da_precios_y_lista_las_villas(self):
        with patch.object(agent, "_bq", _bq_con(self.FILAS)):
            r = agent.consultar_precios("ADOR", "2027-07-01", "2027-07-01")
        self.assertIn("error", r)
        self.assertEqual([], r["noches"])
        self.assertEqual(["ADORA", "ADORABLE"], r["villas_coincidentes"])

    def test_la_consulta_trae_la_villa_de_cada_fila(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq), \
             patch.object(agent, "_nombres_villas", return_value=[]):
            agent.consultar_precios("ADORA", "2027-07-01", "2027-07-01")
        self.assertIn("v.nombre AS villa_nombre", _sql(bq))


class CalendarioDeUnaSolaVillaTest(unittest.TestCase):

    @staticmethod
    def _dia(villa_id, villa, fecha, tipo):
        return _Fila(villa_id=villa_id, villa_nombre=villa, fecha=fecha,
                     tipo_ocupacion=tipo, reserva_id=None,
                     estancia_minima_noches=None)

    def _filas(self):
        return [
            self._dia("v1", "ADORA", "2026-09-20", "Libre"),
            self._dia("v2", "ADORABLE", "2026-09-20", "No Disponible"),
            self._dia("v1", "ADORA", "2026-09-21", "Libre"),
            self._dia("v2", "ADORABLE", "2026-09-21", "No Disponible"),
        ]

    def test_si_un_nombre_casa_exacto_solo_cuenta_sus_dias(self):
        with patch.object(agent, "_bq", _bq_con(self._filas())):
            r = agent.calendario_villa("ADORA", "2026-09-20", "2026-09-21")
        self.assertEqual("ADORA", r["villa"])
        self.assertEqual(2, r["resumen"]["noches_libres"])
        self.assertEqual(0, r["resumen"]["noches_bloqueadas"])
        self.assertEqual(1, len(r["tramos"]))

    def test_si_es_ambiguo_no_mezcla_calendarios(self):
        with patch.object(agent, "_bq", _bq_con(self._filas())):
            r = agent.calendario_villa("ADOR", "2026-09-20", "2026-09-21")
        self.assertIn("error", r)
        self.assertEqual([], r["tramos"])
        self.assertEqual(["ADORA", "ADORABLE"], r["villas_coincidentes"])


# ---------------------------------------------------------------------------
# 2. Cada rol solo menciona herramientas que tiene, y las menciona todas
# ---------------------------------------------------------------------------

class InstruccionesCoherentesConHerramientasTest(unittest.TestCase):
    """ADK lanza ValueError si el modelo llama a una herramienta que su agente
    no tiene, y la conversación muere con 'Tool ... not found'."""

    TODAS = {t.__name__ for a in agent.AGENTS.values() for t in a.tools}

    @staticmethod
    def _mencionadas(instruccion: str, nombres: set[str]) -> set[str]:
        return {n for n in nombres if re.search(rf"\b{n}\b", instruccion)}

    def test_ningun_rol_menciona_herramientas_que_no_tiene(self):
        for rol, ag in agent.AGENTS.items():
            propias = {t.__name__ for t in ag.tools}
            ajenas = self._mencionadas(ag.instruction, self.TODAS) - propias
            self.assertEqual(set(), ajenas, rol)

    def test_cada_rol_conoce_todas_sus_herramientas(self):
        for rol, ag in agent.AGENTS.items():
            propias = {t.__name__ for t in ag.tools}
            olvidadas = propias - self._mencionadas(ag.instruction, propias)
            self.assertEqual(set(), olvidadas, rol)

    def test_quien_tiene_precios_no_lee_que_no_hay_precios(self):
        for rol, ag in agent.AGENTS.items():
            if any(t.__name__ == "consultar_precios" for t in ag.tools):
                self.assertNotIn(
                    "No tenemos información de precios", ag.instruction, rol
                )


# ---------------------------------------------------------------------------
# 3. Disponibilidad
# ---------------------------------------------------------------------------

class DisponibilidadRealTest(unittest.TestCase):

    def setUp(self):
        ahora = datetime.datetime(2026, 7, 19, 16, 0,
                                  tzinfo=ZoneInfo("Europe/Madrid"))
        self.bq = _bq_con([])
        for p in (patch.object(agent, "_ahora_local", return_value=ahora),
                  patch.object(agent, "_bq", self.bq)):
            p.start()
            self.addCleanup(p.stop)

    def _consultar(self):
        agent.consultar_disponibilidad("2026-08-01", "2026-08-08")
        return _sql(self.bq)

    def test_los_bloqueos_del_calendario_cuentan(self):
        sql = self._consultar()
        self.assertIn(agent.TABLA_OCUPACION, sql)
        self.assertIn("'LIBRE'", sql)

    def test_respeta_la_estancia_minima(self):
        sql = self._consultar()
        self.assertIn("estancia_minima_noches", sql)
        self.assertEqual(7, _params(self.bq)["noches"])

    def test_una_reserva_perdida_no_bloquea(self):
        self.assertIn("'PERDIDA'", self._consultar())

    def test_una_reserva_sin_salida_bloquea_su_entrada(self):
        self.assertIn("r.fecha_salida IS NULL", self._consultar())

    def test_tiene_presupuesto_para_la_tabla_diaria(self):
        self._consultar()
        config = self.bq.query.call_args[1]["job_config"]
        self.assertEqual(agent._BILLING_CAP_DIARIO, config.maximum_bytes_billed)


# ---------------------------------------------------------------------------
# 4. consultar_reservas da el total real, no el tamaño de la página
# ---------------------------------------------------------------------------

class TotalDeReservasTest(unittest.TestCase):

    def test_devuelve_el_total_aunque_la_pagina_sea_menor(self):
        filas = [_Fila(localizador="A", total_resultados=57),
                 _Fila(localizador="B", total_resultados=57)]
        bq = _bq_con(filas)
        with patch.object(agent, "_bq", bq):
            r = agent.consultar_reservas(limite=2)
        self.assertEqual(57, r["total"])
        self.assertEqual(2, r["count"])
        self.assertNotIn("total_resultados", r["reservas"][0])
        self.assertIn("COUNT(*) OVER ()", _sql(bq))

    def test_cada_reserva_dice_si_es_del_propietario(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.consultar_reservas()
        self.assertIn("r.subtipo_reserva", _sql(bq))


# ---------------------------------------------------------------------------
# 5. resumen_reservas: solo reservas en firme, meses en orden, monedas
# ---------------------------------------------------------------------------

class ResumenReservasTest(unittest.TestCase):

    def test_por_defecto_no_cuenta_perdidas_prereservas_ni_borradores(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.resumen_reservas()
        sql = _sql(bq)
        for estado in ("'PERDIDA'", "'PRERESERVA'", "'BORRADOR'", "'CANCELACION'"):
            self.assertIn(estado, sql, estado)

    def test_se_puede_pedir_todo(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.resumen_reservas(solo_en_firme=False)
        self.assertNotIn("'PERDIDA'", _sql(bq))

    def test_por_mes_devuelve_los_mas_recientes_en_orden_cronologico(self):
        filas = [_Fila(dimension="2026-09", total_reservas=5, monedas=["EUR"],
                       total_grupos=30),
                 _Fila(dimension="2026-08", total_reservas=9, monedas=["EUR"],
                       total_grupos=30)]
        bq = _bq_con(filas)
        with patch.object(agent, "_bq", bq):
            r = agent.resumen_reservas(agrupar_por="mes", limite=2)
        self.assertIn("ORDER BY dimension DESC", _sql(bq))
        self.assertEqual(["2026-08", "2026-09"],
                         [f["dimension"] for f in r["resumen"]])
        self.assertEqual(30, r["total_grupos"])
        self.assertNotIn("total_grupos", r["resumen"][0])

    def test_sin_fecha_final_los_periodos_recientes_acaban_hoy(self):
        # Con reservas hasta 2028, "los más recientes" eran meses futuros con
        # un puñado de reservas en vez de la evolución real hasta hoy.
        for agrupar, corte in (
            ("mes", "LAST_DAY(CURRENT_DATE('Europe/Madrid'))"),
            ("ano", "LAST_DAY(CURRENT_DATE('Europe/Madrid'), YEAR)"),
        ):
            bq = _bq_con([])
            with patch.object(agent, "_bq", bq):
                agent.resumen_reservas(agrupar_por=agrupar)
            self.assertIn(f"r.fecha_entrada <= {corte}", _sql(bq), agrupar)

    def test_con_fecha_final_no_se_corta_en_hoy(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.resumen_reservas(agrupar_por="mes", fecha_hasta="2028-12-31")
        self.assertNotIn("CURRENT_DATE", _sql(bq))

    def test_por_villa_no_se_corta_en_hoy(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.resumen_reservas(agrupar_por="villa")
        self.assertNotIn("CURRENT_DATE", _sql(bq))

    def test_por_defecto_no_cuenta_las_estancias_del_propietario(self):
        # En 2025 eran 590 de 3.293 "reservas", todas a 0 €.
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.resumen_reservas()
        self.assertIn("'Reserva Propietario'", _sql(bq))

    def test_se_pueden_incluir_las_estancias_del_propietario(self):
        bq = _bq_con([])
        with patch.object(agent, "_bq", bq):
            agent.resumen_reservas(incluir_propietario=True)
        self.assertNotIn("'Reserva Propietario'", _sql(bq))

    def test_avisa_si_los_importes_mezclan_monedas(self):
        filas = [_Fila(dimension="Calpe", total_reservas=5, monedas=["EUR"],
                       total_grupos=2),
                 _Fila(dimension="Altea", total_reservas=3, monedas=["GBP"],
                       total_grupos=2)]
        with patch.object(agent, "_bq", _bq_con(filas)):
            r = agent.resumen_reservas(agrupar_por="zona")
        self.assertIn("aviso_monedas", r)

    def test_una_sola_moneda_no_avisa(self):
        filas = [_Fila(dimension="Calpe", total_reservas=5, monedas=["EUR"],
                       total_grupos=1)]
        with patch.object(agent, "_bq", _bq_con(filas)):
            r = agent.resumen_reservas(agrupar_por="zona")
        self.assertNotIn("aviso_monedas", r)


# ---------------------------------------------------------------------------
# 6. ejecutar_sql no puede salir de silver_clean
# ---------------------------------------------------------------------------

class ValidadorSqlTest(unittest.TestCase):

    def test_rechaza_la_segunda_tabla_de_un_from_con_comas(self):
        self.assertIsNotNone(agent._motivo_rechazo_sql(
            "SELECT * FROM silver_clean.stg_etendo_Villa v, "
            "agent_analytics.chat_turns t"
        ))

    def test_un_comentario_no_esconde_la_tabla(self):
        self.assertIsNotNone(agent._motivo_rechazo_sql(
            "SELECT * FROM /* nada */ agent_analytics.chat_turns"
        ))

    def test_identificadores_entrecomillados_por_partes_no_revientan(self):
        self.assertIsNotNone(agent._motivo_rechazo_sql(
            "SELECT * FROM `abahanaweb`.`agent_analytics`.`chat_turns`"
        ))

    def test_una_lista_de_columnas_con_comas_sigue_valiendo(self):
        self.assertIsNone(agent._motivo_rechazo_sql(
            "SELECT v.nombre, v.zona FROM silver_clean.stg_etendo_Villa v, "
            "UNNEST([1]) AS x"
        ))

    def test_extract_year_from_no_se_confunde_con_una_tabla(self):
        self.assertIsNone(agent._motivo_rechazo_sql(
            "SELECT EXTRACT(YEAR FROM r.fecha_entrada) AS ano "
            "FROM silver_clean.stg_etendo_Reserva r"
        ))


def _job_simulado(tablas, tipo="SELECT", filas=()):
    job = Mock()
    job.statement_type = tipo
    job.referenced_tables = [
        Mock(project=p, dataset_id=d, table_id=t) for p, d, t in tablas
    ]
    job.result.return_value = list(filas)
    return job


class EjecutarSqlConDryRunTest(unittest.TestCase):
    """La lista real de tablas la da BigQuery, no una expresión regular."""

    CONSULTA = "SELECT * FROM silver_clean.vista_rara"

    def test_rechaza_si_bigquery_ve_otra_tabla(self):
        bq = Mock()
        bq.query.return_value = _job_simulado(
            [("abahanaweb", "agent_analytics", "chat_turns")]
        )
        with patch.object(agent, "_bq", bq):
            r = agent.ejecutar_sql(self.CONSULTA)
        self.assertIn("error", r)
        self.assertEqual(1, bq.query.call_count)
        self.assertTrue(bq.query.call_args[1]["job_config"].dry_run)

    def test_rechaza_lo_que_no_es_select(self):
        bq = Mock()
        bq.query.return_value = _job_simulado([], tipo="DELETE")
        with patch.object(agent, "_bq", bq):
            r = agent.ejecutar_sql(self.CONSULTA)
        self.assertIn("error", r)
        self.assertEqual(1, bq.query.call_count)

    def test_ejecuta_si_todo_es_de_silver_clean(self):
        bq = Mock()
        bq.query.side_effect = [
            _job_simulado([("abahanaweb", "silver_clean", "stg_etendo_Villa")]),
            _job_simulado([], filas=[_Fila(nombre="ADORA")]),
        ]
        with patch.object(agent, "_bq", bq):
            r = agent.ejecutar_sql(self.CONSULTA)
        self.assertNotIn("error", r)
        self.assertEqual([{"nombre": "ADORA"}], r["rows"])


if __name__ == "__main__":
    unittest.main()
