"""Tests de las salvaguardas y del formato de salida de las herramientas."""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class _Fila(dict):
    """Imita una Row de BigQuery: se comporta como dict con .items()."""


def _consulta_ejecutada(mock_bq) -> str:
    return mock_bq.query.call_args[0][0]


class UrlPermitidaTest(unittest.TestCase):
    """El filtro de dominio protege un navegador real corriendo en Cloud Run."""

    def test_acepta_el_dominio_y_sus_subdominios(self):
        for url in (
            "https://abahanavillas.com/es/",
            "https://www.abahanavillas.com/es/contacto",
        ):
            self.assertTrue(agent._url_permitida(url), url)

    def test_rechaza_dominios_que_solo_contienen_el_nombre(self):
        for url in (
            "https://abahanavillas.com.atacante.io/robo",
            "https://noabahanavillas.com/",
            "https://abahanavillas.com.evil.example/",
        ):
            self.assertFalse(agent._url_permitida(url), url)

    def test_rechaza_el_truco_del_usuario_en_la_autoridad(self):
        self.assertFalse(
            agent._url_permitida(
                "http://abahanavillas.com@169.254.169.254/latest/meta-data/"
            )
        )

    def test_rechaza_host_interno_y_esquemas_no_https(self):
        self.assertFalse(agent._url_permitida("http://localhost:8080/admin"))
        self.assertFalse(agent._url_permitida("file:///etc/passwd"))
        self.assertFalse(agent._url_permitida("http://www.abahanavillas.com/es/"))

    def test_consultar_web_no_navega_si_la_url_no_pasa(self):
        with patch.object(agent, "_playwright_en_hilo") as navegador:
            resultado = agent.consultar_web("https://abahanavillas.com.atacante.io/x")
        navegador.assert_not_called()
        self.assertIn("error", resultado)


class ConsultaSilverCleanTest(unittest.TestCase):
    """ejecutar_sql solo puede leer silver_clean, no todo el proyecto."""

    def _motivo(self, query):
        return agent._motivo_rechazo_sql(query)

    def test_acepta_consultas_al_dataset_permitido(self):
        for query in (
            "SELECT nombre FROM `abahanaweb.silver_clean.stg_etendo_Villa`",
            "SELECT nombre FROM silver_clean.stg_etendo_Villa",
            "WITH v AS (SELECT 1 FROM silver_clean.stg_etendo_Villa) SELECT * FROM v",
            "SELECT column_name FROM silver_clean.INFORMATION_SCHEMA.COLUMNS",
            "SELECT t.table_name FROM "
            "`abahanaweb.silver_clean.INFORMATION_SCHEMA.TABLES` t",
        ):
            self.assertIsNone(self._motivo(query), query)

    def test_rechaza_las_conversaciones_de_los_usuarios(self):
        motivo = self._motivo("SELECT * FROM `abahanaweb.agent_analytics.chat_turns`")
        self.assertIsNotNone(motivo)
        self.assertIn("silver_clean", motivo)

    def test_rechaza_otros_datasets_y_otros_proyectos(self):
        self.assertIsNotNone(self._motivo("SELECT * FROM `abahanaweb.bronze.raw`"))
        self.assertIsNotNone(
            self._motivo("SELECT * FROM `otro-proyecto.silver_clean.stg_etendo_Villa`")
        )

    def test_rechaza_varias_sentencias(self):
        motivo = self._motivo(
            "SELECT 1; DELETE FROM `abahanaweb.silver_clean.stg_etendo_Villa` "
            "WHERE TRUE"
        )
        self.assertIsNotNone(motivo)

    def test_tolera_el_punto_y_coma_final(self):
        self.assertIsNone(
            self._motivo("SELECT nombre FROM silver_clean.stg_etendo_Villa;")
        )

    def test_sigue_rechazando_escrituras(self):
        for query in ("DELETE FROM x", "DROP TABLE y", "UPDATE z SET a = 1"):
            self.assertIsNotNone(self._motivo(query), query)

    def test_ejecutar_sql_no_llega_a_bigquery_si_rechaza(self):
        with patch.object(agent, "_bq") as bq:
            resultado = agent.ejecutar_sql(
                "SELECT * FROM `abahanaweb.agent_analytics.chat_turns`"
            )
        bq.query.assert_not_called()
        self.assertIn("error", resultado)
        self.assertEqual([], resultado["rows"])


class BusquedaSinRecorteTest(unittest.TestCase):
    """No se corta el resultado y se informa del total exacto."""

    def setUp(self):
        self.bq = Mock()
        filas = [
            _Fila(nombre="Villa Uno", total_resultados=2),
            _Fila(nombre="Villa Dos", total_resultados=2),
        ]
        self.bq.query.return_value.result.return_value = filas
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_buscar_propiedades_no_lleva_limite_fijo(self):
        agent.buscar_propiedades(ubicacion="Calpe")
        self.assertNotIn("LIMIT 20", _consulta_ejecutada(self.bq))

    def test_buscar_propiedades_devuelve_el_total(self):
        resultado = agent.buscar_propiedades(ubicacion="Calpe")
        self.assertEqual(2, resultado["total"])
        self.assertEqual(2, resultado["count"])
        self.assertNotIn("total_resultados", resultado["matches"][0])

    def test_buscar_por_valoracion_tampoco_recorta(self):
        agent.buscar_por_valoracion(rating_min=4)
        self.assertNotIn("LIMIT 20", _consulta_ejecutada(self.bq))

    def test_listar_propiedades_usa_la_misma_consulta_que_buscar(self):
        agent.listar_propiedades()
        catalogo = _consulta_ejecutada(self.bq)
        self.bq.reset_mock()
        agent.buscar_propiedades()
        self.assertEqual(catalogo, _consulta_ejecutada(self.bq))


class DeduplicacionCoherenteTest(unittest.TestCase):
    """Activo/visible se filtra ANTES de deduplicar, en todas las consultas."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _orden_correcto(self, sql: str) -> bool:
        cuerpo = sql[: sql.index("QUALIFY")]
        return "es_activo = TRUE" in cuerpo and "es_visible = TRUE" in cuerpo

    def test_todas_las_busquedas_filtran_antes_del_qualify(self):
        for llamada in (
            agent.listar_propiedades,
            agent.buscar_propiedades,
            agent.buscar_por_valoracion,
            # La de disponibilidad es la que más importa: no puede ofrecer como
            # libre una villa que no sale ni en el catálogo.
            lambda: agent.consultar_disponibilidad("2099-01-01"),
        ):
            self.bq.reset_mock()
            llamada()
            self.assertTrue(
                self._orden_correcto(_consulta_ejecutada(self.bq)),
                f"{getattr(llamada, '__name__', 'consulta')} deduplica antes de filtrar",
            )


class FiltroAmenidadesTest(unittest.TestCase):
    """Pedir 'sin X' no puede descartar las villas sin ficha técnica."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exigir_la_amenidad_compara_contra_true(self):
        agent.buscar_propiedades(internet=True)
        self.assertIn("f.tiene_internet = TRUE", _consulta_ejecutada(self.bq))

    def test_no_exigirla_trata_el_dato_ausente_como_ausencia(self):
        agent.buscar_propiedades(lavavajillas=False)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("COALESCE(f.tiene_lavavajillas, FALSE) = FALSE", sql)


class DesglosePorPlantaTest(unittest.TestCase):
    """Cada estancia y cada baño cuelgan de una planta: el desglose debe
    conservar esa dimensión, no aplastarla en un total por villa."""

    def _plantas(self):
        comun = dict(
            camas_dobles=0, literas=0, camas_nido=0, camas_partidas=0,
            banios_con_banera=0, banios_con_ducha=0, banios_con_jacuzzi=0,
            banios_con_bide=0, estancias_con_sofacama=0,
        )
        return [
            _Fila(villa_id="v1", numero_planta=0, planta="SOTANO",
                  planta_tiene_jacuzzi=False, planta_tiene_sauna=True,
                  banios=1, banios_ensuite=0, dormitorios=0, camas_totales=0,
                  camas_king_size=0, camas_simples=0, **comun),
            _Fila(villa_id="v1", numero_planta=1, planta="PLANTA PRINCIPAL",
                  planta_tiene_jacuzzi=False, planta_tiene_sauna=False,
                  banios=3, banios_ensuite=2, dormitorios=2, camas_totales=3,
                  camas_king_size=2, camas_simples=1, **comun),
            _Fila(villa_id="v1", numero_planta=2, planta="PLANTA ALTA",
                  planta_tiene_jacuzzi=False, planta_tiene_sauna=False,
                  banios=2, banios_ensuite=1, dormitorios=2, camas_totales=2,
                  camas_king_size=2, camas_simples=0, **comun),
        ]

    def _resultado(self):
        villa = _Fila(villa_id="v1", nombre="ALEGRIA", capacidad_pax=8)
        bq = Mock()
        bq.query.side_effect = [
            Mock(**{"result.return_value": [villa]}),
            Mock(**{"result.return_value": self._plantas()}),
        ]
        with patch.object(agent, "_bq", bq):
            return agent.obtener_detalle_propiedad("ALEGRIA"), bq

    def test_devuelve_los_banios_de_cada_planta(self):
        resultado, _ = self._resultado()
        plantas = resultado["matches"][0]["plantas"]
        self.assertEqual(
            [("SOTANO", 1), ("PLANTA PRINCIPAL", 3), ("PLANTA ALTA", 2)],
            [(p["planta"], p["banios"]) for p in plantas],
        )

    def test_cada_planta_lleva_sus_dormitorios_y_camas(self):
        resultado, _ = self._resultado()
        alta = resultado["matches"][0]["plantas"][2]
        self.assertEqual(2, alta["dormitorios"])
        self.assertEqual(2, alta["camas_totales"])
        self.assertTrue(resultado["matches"][0]["plantas"][0]["planta_tiene_sauna"])

    def test_los_totales_de_villa_son_la_suma_de_las_plantas(self):
        ficha = self._resultado()[0]["matches"][0]
        self.assertEqual(6, ficha["banios_con_detalle"])
        self.assertEqual(3, ficha["banios_ensuite"])
        self.assertEqual(4, ficha["dormitorios"])
        self.assertEqual(5, ficha["camas_totales"])

    def test_la_consulta_enlaza_planta_con_banios_y_estancias(self):
        _, bq = self._resultado()
        consulta = bq.query.call_args_list[1][0][0]
        self.assertIn("stg_etendo_Planta", consulta)
        self.assertIn("stg_etendo_OV_Banios", consulta)
        self.assertIn("stg_etendo_Estancia", consulta)
        self.assertIn("p.villa_id IN UNNEST(@villa_ids)", consulta)

    def test_si_falla_el_desglose_la_ficha_sigue_saliendo(self):
        villa = _Fila(villa_id="v1", nombre="ALEGRIA")
        bq = Mock()
        bq.query.side_effect = [
            Mock(**{"result.return_value": [villa]}),
            RuntimeError("Planta no disponible"),
        ]
        with patch.object(agent, "_bq", bq), self.assertLogs(
            "agente-villas", level="WARNING"
        ):
            resultado = agent.obtener_detalle_propiedad("ALEGRIA")
        self.assertEqual(1, resultado["count"])
        self.assertFalse(resultado["detalle_banios_disponible"])
        self.assertNotIn("plantas", resultado["matches"][0])


class HabitacionesYCamasTest(unittest.TestCase):
    """`capacidad_camas` eran habitaciones (191/204). Las camas de verdad solo
    están en stg_etendo_Estancia."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)
        # La columna renombrada ya existe salvo que un test diga lo contrario.
        col = patch.object(agent, "_columna_habitaciones",
                           return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def test_la_busqueda_ya_no_expone_capacidad_camas(self):
        agent.buscar_propiedades()
        sql = _consulta_ejecutada(self.bq)
        self.assertNotIn("capacidad_camas", sql)
        self.assertIn("AS numero_habitaciones", sql)

    def test_la_busqueda_expone_las_camas_reales(self):
        agent.buscar_propiedades()
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("stg_etendo_Estancia", sql)
        self.assertIn("camas_totales", sql)

    def test_habitaciones_min_filtra_por_habitaciones(self):
        agent.buscar_propiedades(habitaciones_min=4)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("v.numero_habitaciones >= @habitaciones_min", sql)

    def test_camas_min_filtra_por_las_camas_de_estancia(self):
        agent.buscar_propiedades(camas_min=6)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("c.camas_totales >= @camas_min", sql)
        self.assertNotIn("capacidad_camas", sql)


class DisponibilidadPorHabitacionesTest(unittest.TestCase):
    """La sugerencia que trae la interfaz es "villas DISPONIBLES con 4
    habitaciones": el filtro tiene que existir también aquí."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)
        col = patch.object(agent, "_columna_habitaciones",
                           return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def test_filtra_por_habitaciones(self):
        agent.consultar_disponibilidad("2099-07-01", habitaciones_min=4)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("v.numero_habitaciones >= @habitaciones_min", sql)

    def test_filtra_por_camas_reales(self):
        agent.consultar_disponibilidad("2099-07-01", camas_min=8)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("c.camas_totales >= @camas_min", sql)
        self.assertIn("stg_etendo_Estancia", sql)


class ColumnaHabitacionesTest(unittest.TestCase):
    """Dataform renombra la columna; hasta que se ejecute, sigue la vieja."""

    def setUp(self):
        agent._columna_habitaciones.cache_clear()
        self.addCleanup(agent._columna_habitaciones.cache_clear)

    def _con_columnas(self, *nombres):
        bq = Mock()
        bq.query.return_value.result.return_value = [
            _Fila(column_name=n) for n in nombres
        ]
        return bq

    def test_usa_el_nombre_nuevo_cuando_existe(self):
        with patch.object(agent, "_bq", self._con_columnas("numero_habitaciones")):
            self.assertEqual("numero_habitaciones", agent._columna_habitaciones())

    def test_cae_al_nombre_viejo_si_aun_no_se_ha_migrado(self):
        with patch.object(agent, "_bq", self._con_columnas("capacidad_camas")):
            self.assertEqual("capacidad_camas", agent._columna_habitaciones())

    def test_si_no_puede_consultarlo_no_rompe_la_busqueda(self):
        bq = Mock()
        bq.query.side_effect = RuntimeError("sin acceso")
        with patch.object(agent, "_bq", bq):
            self.assertIn(
                agent._columna_habitaciones(),
                ("numero_habitaciones", "capacidad_camas"),
            )


class EtiquetasYRolesTest(unittest.TestCase):
    def test_cada_codigo_acepta_sus_grafias(self):
        self.assertIn("BLOQUEADA", agent._ESTADOS_RESERVA["BO"])
        self.assertIn("CONFIRMADA", agent._ESTADOS_DOCUMENTO["CO"])
        # Se conservan las grafías que ya casaban con los datos de silver.
        self.assertIn("BORRADOR", agent._ESTADOS_RESERVA["BO"])
        self.assertIn("COMPLETADA", agent._ESTADOS_DOCUMENTO["CO"])

    def test_el_docstring_de_valoracion_dice_lo_que_calcula(self):
        doc = agent.buscar_por_valoracion.__doc__
        self.assertIn("vistas", doc)
        self.assertNotIn("cocina", doc)

    def test_el_cliente_no_ve_el_feedback_de_otros_usuarios(self):
        nombres = {t.__name__ for t in agent.agent_cliente.tools}
        self.assertNotIn("consultar_feedback_negativo", nombres)

    def test_interno_y_admin_si_lo_conservan(self):
        for ag in (agent.agent_interno, agent.agent_admin):
            nombres = {t.__name__ for t in ag.tools}
            self.assertIn("consultar_feedback_negativo", nombres)


if __name__ == "__main__":
    unittest.main()
