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


class PaginaNoEncontradaTest(unittest.TestCase):
    """Adivinar una URL que no existe devolvía el HTML de la página de error
    con el título de la portada, así que el agente lo daba por bueno."""

    def test_un_404_se_reporta_como_error(self):
        with patch.object(agent, "_playwright_en_hilo", return_value=(404, "<html/>")):
            resultado = agent.consultar_web(
                "https://www.abahanavillas.com/es/no-existe"
            )
        self.assertIn("error", resultado)
        self.assertIn("404", resultado["error"])
        self.assertNotIn("contenido", resultado)

    def test_una_pagina_valida_se_devuelve(self):
        html = "<html><head><title>Condiciones</title></head><body>" + (
            "<p>Cargo adicional unico de 70 euros por reserva.</p>" * 2
        ) + "</body></html>"
        agent._CACHE_WEB.clear()
        with patch.object(agent, "_playwright_en_hilo", return_value=(200, html)):
            resultado = agent.consultar_web(
                "https://www.abahanavillas.com/es/condiciones-generales-uso-y-alquiler"
            )
        self.assertNotIn("error", resultado)
        self.assertIn("70 euros", resultado["contenido"])


class BuscarPaginaWebTest(unittest.TestCase):
    """Cuando una URL clave falla, hay que poder localizar la página en el
    sitemap en vez de seguir adivinando."""

    SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset>
      <url><loc>https://www.abahanavillas.com/es/contacto</loc></url>
      <url><loc>https://www.abahanavillas.com/es/aviso-legal</loc></url>
      <url><loc>https://www.abahanavillas.com/es/ayuda-y-preguntas-frecuentes/preguntas-frecuentes-del-usuario</loc></url>
      <url><loc>https://www.abahanavillas.com/en/holiday-rentals</loc></url>
    </urlset>"""

    def setUp(self):
        agent._sitemap_urls.cache_clear()
        self.addCleanup(agent._sitemap_urls.cache_clear)
        respuesta = Mock(text=self.SITEMAP)
        respuesta.raise_for_status.return_value = None
        self.get = patch.object(agent.requests, "get", return_value=respuesta)
        self.get.start()
        self.addCleanup(self.get.stop)

    def test_encuentra_las_faqs(self):
        r = agent.buscar_pagina_web("preguntas frecuentes usuario")
        urls = [p["url"] for p in r["paginas"]]
        self.assertIn(
            "https://www.abahanavillas.com/es/ayuda-y-preguntas-frecuentes/"
            "preguntas-frecuentes-del-usuario",
            urls,
        )

    def test_solo_devuelve_paginas_en_espanol(self):
        r = agent.buscar_pagina_web("holiday rentals")
        self.assertTrue(all("/es/" in p["url"] for p in r["paginas"]), r["paginas"])

    def test_sin_coincidencias_no_inventa(self):
        r = agent.buscar_pagina_web("zzzz inexistente qqqq")
        self.assertEqual([], r["paginas"])

    def test_si_el_sitemap_falla_devuelve_error_y_no_rompe(self):
        agent._sitemap_urls.cache_clear()
        with patch.object(agent.requests, "get", side_effect=RuntimeError("caído")):
            r = agent.buscar_pagina_web("contacto")
        self.assertEqual([], r["paginas"])
        self.assertIn("error", r)


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
        self.assertIn("v.tiene_internet = TRUE", _consulta_ejecutada(self.bq))

    def test_no_exigirla_trata_el_dato_ausente_como_ausencia(self):
        agent.buscar_propiedades(lavavajillas=False)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("COALESCE(v.tiene_lavavajillas, FALSE) = FALSE", sql)


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


def _tarifa(fecha, nombre, tipo, es_venta, precio, **extra):
    base = dict(fecha=fecha, estado="0", nombre=nombre, tipo=tipo,
                es_venta=es_venta, precio_final=precio, descuento_pct=0.0,
                estancia_minima_noches=0)
    base.update(extra)
    return _Fila(**base)


class ConsultarPreciosTest(unittest.TestCase):
    """Los precios están en TarifaDia, que no tiene fecha propia: enlaza con
    Ocupacion por ocupacion_id."""

    def setUp(self):
        self.bq = Mock()
        filas = [
            _tarifa("2027-07-01", "PrecioVilla", "std", True, 439.0),
            _tarifa("2027-07-01", "PrecioVilla", "std", False, 307.0),
            _tarifa("2027-07-01", "PrecioVilla", "lt", True, 351.2,
                    descuento_pct=20.0, estancia_minima_noches=27),
            _tarifa("2027-07-01", "Aire Acondicionado", "std", True, 60.0),
            _tarifa("2027-07-01", "Aire Acondicionado", "std", False, 36.0),
            _tarifa("2027-07-02", "PrecioVilla", "std", True, 461.0),
            _tarifa("2027-07-02", "PrecioVilla", "std", False, 322.0),
        ]
        self.bq.query.return_value.result.return_value = filas
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_devuelve_venta_compra_y_margen_por_noche(self):
        r = agent.consultar_precios("ADORA", "2027-07-01", "2027-07-03")
        primera = r["noches"][0]
        self.assertEqual("2027-07-01", primera["fecha"])
        self.assertEqual(439.0, primera["precio_venta"])
        self.assertEqual(307.0, primera["precio_compra"])
        self.assertEqual(132.0, primera["margen"])
        self.assertEqual(30.07, primera["margen_pct"])   # 132/439

    def test_resume_el_periodo(self):
        r = agent.consultar_precios("ADORA", "2027-07-01", "2027-07-03")
        res = r["resumen"]
        self.assertEqual(2, res["noches_con_precio"])
        self.assertEqual(900.0, res["total_venta"])
        self.assertEqual(629.0, res["total_compra"])
        self.assertEqual(271.0, res["margen_total"])

    def test_separa_la_tarifa_de_larga_estancia(self):
        r = agent.consultar_precios("ADORA", "2027-07-01", "2027-07-03")
        lt = r["larga_estancia"]
        self.assertEqual(20.0, lt["descuento_pct"])
        self.assertEqual(27, lt["minimo_noches"])
        self.assertEqual(351.2, lt["precio_venta_medio"])

    def test_lista_los_extras_con_su_margen(self):
        r = agent.consultar_precios("ADORA", "2027-07-01", "2027-07-03")
        extras = {e["concepto"]: e for e in r["extras"]}
        self.assertIn("Aire Acondicionado", extras)
        self.assertEqual(60.0, extras["Aire Acondicionado"]["precio_venta_medio"])
        self.assertEqual(36.0, extras["Aire Acondicionado"]["precio_compra_medio"])
        self.assertNotIn("PrecioVilla", extras)

    def test_excluye_las_filas_sin_es_venta_para_no_contar_doble(self):
        agent.consultar_precios("ADORA", "2027-07-01", "2027-07-03")
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("t.es_venta IS NOT NULL", sql)

    def test_rechaza_rangos_desmedidos_sin_ir_a_bigquery(self):
        self.bq.reset_mock()
        r = agent.consultar_precios("ADORA", "2027-01-01", "2027-12-31")
        self.bq.query.assert_not_called()
        self.assertIn("error", r)

    def test_valida_las_fechas(self):
        r = agent.consultar_precios("ADORA", "no-es-fecha")
        self.assertIn("error", r)


class CalendarioVillaTest(unittest.TestCase):
    """El calendario se lee por tramos, no día a día, y distingue el canal."""

    def setUp(self):
        def dia(fecha, tipo, reserva=None):
            return _Fila(fecha=fecha, tipo_ocupacion=tipo, reserva_id=reserva,
                         estancia_minima_noches=4)
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = [
            dia("2026-09-08", "Libre"),
            dia("2026-09-09", "Libre"),
            dia("2026-09-10", "Reserva Agencia", "r1"),
            dia("2026-09-11", "Reserva Agencia", "r1"),
            dia("2026-09-12", "Reserva Agencia", "r1"),
            dia("2026-09-13", "No Disponible"),
        ]
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agrupa_los_dias_consecutivos_en_tramos(self):
        r = agent.calendario_villa("ADORA", "2026-09-08", "2026-09-13")
        tramos = [(t["desde"], t["hasta"], t["tipo_ocupacion"], t["noches"])
                  for t in r["tramos"]]
        self.assertEqual(
            [("2026-09-08", "2026-09-09", "Libre", 2),
             ("2026-09-10", "2026-09-12", "Reserva Agencia", 3),
             ("2026-09-13", "2026-09-13", "No Disponible", 1)],
            tramos,
        )

    def test_resume_noches_por_estado_y_ocupacion(self):
        r = agent.calendario_villa("ADORA", "2026-09-08", "2026-09-13")
        res = r["resumen"]
        self.assertEqual(3, res["noches_ocupadas"])
        self.assertEqual(2, res["noches_libres"])
        self.assertEqual(1, res["noches_bloqueadas"])
        # 3 ocupadas sobre 5 comercializables (se excluyen las bloqueadas)
        self.assertEqual(60.0, res["ocupacion_pct"])

    def test_desglosa_el_canal(self):
        r = agent.calendario_villa("ADORA", "2026-09-08", "2026-09-13")
        self.assertEqual({"Reserva Agencia": 3}, r["resumen"]["por_canal"])

    def test_no_presenta_checkin_como_evento(self):
        agent.calendario_villa("ADORA", "2026-09-08", "2026-09-13")
        sql = _consulta_ejecutada(self.bq)
        self.assertNotIn("es_checkin", sql)
        self.assertNotIn("es_checkout", sql)

    def test_valida_el_rango(self):
        r = agent.calendario_villa("ADORA", "2026-01-01", "2026-12-31")
        self.assertIn("error", r)


class SeccionesDeFichaTest(unittest.TestCase):
    """La ficha da lo básico; el resto solo si el usuario lo pide."""

    def setUp(self):
        self.bq = Mock()
        villa = _Fila(villa_id="v1", codigo_busqueda="123", nombre="ADORA",
                      capacidad_pax=10)
        self.bq.query.side_effect = [
            Mock(**{"result.return_value": [villa]}),   # villa con su ficha
            Mock(**{"result.return_value": []}),        # plantas
        ]
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sql_ficha(self):
        # La ficha va en la misma consulta que la villa.
        return self.bq.query.call_args_list[0][0][0]

    def test_por_defecto_no_arrastra_las_secciones_extra(self):
        agent.obtener_detalle_propiedad("ADORA")
        sql = self._sql_ficha()
        for columna in ("distancia_mar_m", "tiene_gimnasio", "importe_fianza",
                        "alarma_activacion", "wifi_red"):
            self.assertNotIn(columna, sql, columna)

    def test_por_defecto_si_trae_las_amenidades_de_siempre(self):
        agent.obtener_detalle_propiedad("ADORA")
        self.assertIn("tiene_internet", self._sql_ficha())

    def test_una_seccion_pedida_se_incluye(self):
        agent.obtener_detalle_propiedad("ADORA", secciones=["distancias"])
        sql = self._sql_ficha()
        self.assertIn("distancia_mar_m", sql)
        self.assertNotIn("tiene_gimnasio", sql)

    def test_se_pueden_pedir_varias(self):
        agent.obtener_detalle_propiedad("ADORA", secciones=["ocio", "piscina"])
        sql = self._sql_ficha()
        self.assertIn("tiene_gimnasio", sql)
        self.assertIn("piscina_largo_m", sql)

    def test_una_seccion_inexistente_no_rompe_ni_inventa(self):
        r = agent.obtener_detalle_propiedad("ADORA", secciones=["inventada"])
        self.assertEqual(1, r["count"])
        self.assertIn("secciones_disponibles", r)

    def test_acceso_y_seguridad_existe_pero_hay_que_pedirla(self):
        self.assertIn("acceso_seguridad", agent._SECCIONES_FICHA)
        agent.obtener_detalle_propiedad("ADORA")
        self.assertNotIn("alarma_activacion", self._sql_ficha())
        self.bq.reset_mock()
        self.bq.query.side_effect = [
            Mock(**{"result.return_value": [_Fila(villa_id="v1", codigo_busqueda="123")]}),
            Mock(**{"result.return_value": []}),
        ]
        agent.obtener_detalle_propiedad("ADORA", secciones=["acceso_seguridad"])
        self.assertIn("alarma_activacion", self._sql_ficha())


class PreciosPorDefectoTest(unittest.TestCase):
    """Preguntar el precio de una villa no debería exigir fechas."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sin_fechas_usa_los_proximos_dias(self):
        r = agent.consultar_precios("ADORA")
        self.assertNotIn("error", r)
        self.assertIn("periodo", r)
        params = {p.name: p.value for p in
                  self.bq.query.call_args[1]["job_config"].query_parameters}
        self.assertEqual(agent._ahora_local().date(), params["desde"])
        self.assertGreater(params["hasta"], params["desde"])


class ValoracionesDeLaFichaTest(unittest.TestCase):
    """La media debe salir de baños, cocina, interior y exterior, que es lo
    que decía el docstring original y lo que hay en la ficha técnica."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_la_media_usa_las_cuatro_de_la_ficha(self):
        agent.buscar_por_valoracion()
        sql = _consulta_ejecutada(self.bq)
        for col in ("score_rating_banos", "score_rating_cocina",
                    "score_rating_interior", "score_rating_exterior"):
            self.assertIn(col, sql, col)

    def test_ya_no_promedia_las_de_la_tabla_villa(self):
        agent.buscar_por_valoracion()
        cuerpo = _consulta_ejecutada(self.bq)
        cuerpo = cuerpo[cuerpo.index("rating_medio") - 900:cuerpo.index("rating_medio")]
        self.assertNotIn("v.rating_vistas", cuerpo)

    def test_expone_tambien_las_demas_valoraciones(self):
        agent.buscar_por_valoracion()
        sql = _consulta_ejecutada(self.bq)
        for col in ("score_rating_privacidad", "score_rating_tranquilidad",
                    "score_rating_distancia_mar", "score_rating_vistas"):
            self.assertIn(col, sql, col)

    def test_el_docstring_vuelve_a_decir_la_verdad(self):
        doc = agent.buscar_por_valoracion.__doc__
        for palabra in ("baños", "cocina", "interior", "exterior"):
            self.assertIn(palabra, doc, palabra)


class FiltrosDeFichaEnBusquedaTest(unittest.TestCase):
    """Datos que están en la ficha pero no se podían filtrar."""

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

    def test_filtra_por_vista_al_mar(self):
        agent.buscar_propiedades(vista_mar=True)
        self.assertIn("v.tiene_vista_mar = TRUE", _consulta_ejecutada(self.bq))

    def test_filtra_por_distancia_maxima_al_mar(self):
        agent.buscar_propiedades(distancia_mar_max_m=1000)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("v.distancia_mar_m <= @distancia_mar_max_m", sql)

    def test_filtra_por_zona_tranquila_gimnasio_y_accesibilidad(self):
        agent.buscar_propiedades(zona_tranquila=True, gimnasio=True, accesible=True)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("v.zona_tranquila = TRUE", sql)
        self.assertIn("v.tiene_gimnasio = TRUE", sql)
        self.assertIn("v.apto_movilidad_reducida = TRUE", sql)

    def test_la_distancia_cero_es_sin_dato_no_primera_linea(self):
        agent.buscar_propiedades(distancia_mar_max_m=1000)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("v.distancia_mar_m > 0", sql)
        self.assertIn("NULLIF(v.distancia_mar_m, 0)", sql)

    def test_pedir_lo_contrario_no_descarta_las_villas_sin_ficha(self):
        agent.buscar_propiedades(vista_mar=False)
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("COALESCE(v.tiene_vista_mar, FALSE) = FALSE", sql)


class IngresosPorVillaTest(unittest.TestCase):
    """resumen_reservas ya daba la facturación por villa; le faltaba poder
    centrarse en una villa y el ingreso por noche."""

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        patcher = patch.object(agent, "_bq", self.bq)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _params(self):
        return {p.name: p.value for p in
                self.bq.query.call_args[1]["job_config"].query_parameters}

    def test_filtra_por_una_villa(self):
        agent.resumen_reservas(villa_nombre="ADORA")
        self.assertIn("LOWER(r.villa_nombre) LIKE LOWER(@villa_nombre)",
                      _consulta_ejecutada(self.bq))
        self.assertEqual("%ADORA%", self._params()["villa_nombre"])

    def test_devuelve_noches_totales_e_ingreso_por_noche(self):
        agent.resumen_reservas()
        sql = _consulta_ejecutada(self.bq)
        self.assertIn("AS noches_totales", sql)
        self.assertIn("AS importe_por_noche", sql)

    def test_las_fechas_incoherentes_no_restan_noches(self):
        # Hay reservas con salida anterior o igual a la entrada: sin acotar,
        # restarían noches y dispararían el ingreso por noche.
        agent.resumen_reservas()
        self.assertIn("GREATEST(DATE_DIFF(r.fecha_salida, r.fecha_entrada, DAY), 0)",
                      _consulta_ejecutada(self.bq))


class EtiquetasYRolesTest(unittest.TestCase):
    def test_cada_codigo_acepta_sus_grafias(self):
        self.assertIn("BLOQUEADA", agent._ESTADOS_RESERVA["BO"])
        self.assertIn("CONFIRMADA", agent._ESTADOS_DOCUMENTO["CO"])
        # Se conservan las grafías que ya casaban con los datos de silver.
        self.assertIn("BORRADOR", agent._ESTADOS_RESERVA["BO"])
        self.assertIn("COMPLETADA", agent._ESTADOS_DOCUMENTO["CO"])

    def test_el_cliente_no_ve_el_feedback_de_otros_usuarios(self):
        nombres = {t.__name__ for t in agent.agent_cliente.tools}
        self.assertNotIn("consultar_feedback_negativo", nombres)

    def test_interno_y_admin_si_lo_conservan(self):
        for ag in (agent.agent_interno, agent.agent_admin):
            nombres = {t.__name__ for t in ag.tools}
            self.assertIn("consultar_feedback_negativo", nombres)



class _ConBigQueryFalso(unittest.TestCase):

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

    def _sql(self):
        return _consulta_ejecutada(self.bq)

    def _params(self):
        return {p.name: p.value for p in
                self.bq.query.call_args[1]["job_config"].query_parameters}


class ReservasPorSalidaYOcupacionTest(_ConBigQueryFalso):
    """"¿Qué sale hoy?", "¿qué está ocupado?": antes solo había fecha de entrada."""

    def test_filtra_por_fecha_de_salida(self):
        agent.consultar_reservas(salida_desde="2026-09-21", salida_hasta="2026-09-21")
        sql = self._sql()
        self.assertIn("r.fecha_salida >= @salida_desde", sql)
        self.assertIn("r.fecha_salida <= @salida_hasta", sql)
        self.assertNotIn("r.fecha_entrada >= @fecha_desde", sql)
        self.assertIn("ORDER BY r.fecha_salida", sql)

    def test_ocupada_en_una_fecha_no_cuenta_la_noche_de_salida(self):
        agent.consultar_reservas(activa_en="2026-09-21")
        self.assertIn("r.fecha_entrada <= @activa_en AND r.fecha_salida > @activa_en",
                      self._sql())

    def test_por_defecto_una_perdida_no_es_una_reserva(self):
        agent.consultar_reservas(activa_en="2026-09-21")
        self.assertIn("'PE', 'PERDIDA'", self._sql())

    def test_devuelve_anulacion_y_estado_de_limpieza(self):
        agent.consultar_reservas()
        sql = self._sql()
        self.assertIn("r.fecha_anulacion", sql)
        self.assertIn("r.estado_limpieza", sql)


class CanceladasTest(_ConBigQueryFalso):
    """"Canceladas en los últimos 30 días" daba 6 o ninguna según la llamada."""

    def test_filtra_por_fecha_de_anulacion_y_no_las_excluye(self):
        agent.consultar_reservas(anulada_desde="2026-08-22", estado_reserva="CA")
        sql = self._sql()
        self.assertIn("r.fecha_anulacion >= @anulada_desde", sql)
        self.assertNotIn("NOT IN ('CA', 'CANCELACION', 'CANCELADA'", sql)
        self.assertIn("ORDER BY r.fecha_anulacion DESC", sql)

    def test_pedir_un_estado_no_queda_anulado_por_el_filtro_por_defecto(self):
        agent.consultar_reservas(estado_reserva="CA")
        sql = self._sql()
        self.assertIn("UPPER(r.estado_reserva) IN", sql)
        self.assertNotIn("NOT IN ('CA', 'CANCELACION', 'CANCELADA'", sql)


class EquipamientoYDireccionTest(_ConBigQueryFalso):
    """Jacuzzi, billar, parking o una calle: datos que había y no se filtraban."""

    def test_jacuzzi_billar_y_futbolin_van_por_caracteristicas(self):
        # Con parámetros sueltos el modelo creía que la lista era completa y
        # decía "no hay filtro para sauna": todo va por `caracteristicas`.
        import inspect
        for herramienta in (agent.buscar_propiedades, agent.consultar_disponibilidad):
            parametros = inspect.signature(herramienta).parameters
            self.assertIn("caracteristicas", parametros)
            for suelto in ("jacuzzi", "billar", "futbolin", "garaje"):
                self.assertNotIn(suelto, parametros)

    def test_parking_vale_cualquier_aparcamiento(self):
        agent.buscar_propiedades(parking=True)
        sql = self._sql()
        for columna in ("tiene_garaje", "tiene_parking_calle",
                        "tiene_parking_cubierto", "tiene_parking_descubierto"):
            self.assertIn(f"v.{columna}", sql)

    def test_la_barbacoa_va_por_caracteristicas(self):
        # Como jacuzzi o billar: sin parámetro suelto.
        self.assertNotIn("barbacoa", agent._EQUIPAMIENTO)

    def test_la_direccion_ignora_el_tipo_de_via(self):
        agent.buscar_propiedades(direccion="Calle Kabul 7")
        valores = self._params()
        self.assertEqual("%kabul%", valores["direccion_0"])
        self.assertNotIn("%calle%", valores.values())

    def test_el_numero_de_la_calle_no_encaja_en_otro_numero(self):
        agent.buscar_propiedades(direccion="Cabo de Palos 4")
        numero = [v for v in self._params().values() if "4" in v][0]
        import re
        self.assertRegex("Calle Cabo de Palos 4 - 03724", numero)
        self.assertIsNone(re.search(numero, "Calle Cabo de Palos 14 - 03710"))
        self.assertIn("REGEXP_CONTAINS(v.direccion", self._sql())

    def test_la_disponibilidad_filtra_por_equipamiento_y_direccion(self):
        agent.consultar_disponibilidad(
            fecha_desde="2099-10-09", fecha_hasta="2099-10-12",
            vista_mar=True, admite_animales=True, parking=True,
            direccion="Cumbre del Sol")
        sql = self._sql()
        self.assertIn("v.tiene_vista_mar = TRUE", sql)
        self.assertIn("v.admite_animales = TRUE", sql)
        self.assertIn("v.tiene_parking_calle", sql)
        self.assertIn("LOWER(v.direccion) LIKE @direccion_0", sql)


class SinRazonamientoTest(unittest.TestCase):

    def test_los_tres_agentes_responden_sin_razonamiento_previo(self):
        for rol, ag in agent.AGENTS.items():
            self.assertEqual(
                0, ag.generate_content_config.thinking_config.thinking_budget, rol)


class FechaDeHoyTest(unittest.TestCase):
    """Sin razonamiento, "del 3 al 10 de octubre" salía como octubre de 2024."""

    def test_la_fecha_de_hoy_va_en_las_instrucciones(self):
        import datetime
        from google.adk.models import LlmRequest
        peticion = LlmRequest()
        ahora = datetime.datetime(2026, 9, 21, 10, 30, tzinfo=agent._TIMEZONE)
        with patch.object(agent, "_ahora_local", return_value=ahora):
            self.assertIsNone(agent.fecha_de_hoy(None, peticion))
        texto = str(peticion.config.system_instruction)
        self.assertIn("lunes 21 de septiembre de 2026 (2026-09-21)", texto)
        self.assertIn("septiembre (desde hoy) a diciembre son de 2026", texto)
        self.assertIn("enero a agosto, de 2027", texto)
        self.assertIn('"del 3 al 10 de octubre" es del 2026-10-03 al 2026-10-10', texto)
        self.assertIn("Un día de septiembre anterior a hoy es de 2027", texto)

    def test_en_diciembre_el_ejemplo_es_de_enero_del_ano_siguiente(self):
        import datetime
        texto = agent._texto_fecha_de_hoy(
            datetime.datetime(2026, 12, 5, 9, 0, tzinfo=agent._TIMEZONE))
        self.assertIn("diciembre (desde hoy) es de 2026", texto)
        self.assertIn('"del 3 al 10 de enero" es del 2027-01-03 al 2027-01-10', texto)

    def test_los_tres_agentes_la_reciben(self):
        for rol, ag in agent.AGENTS.items():
            self.assertIs(agent.fecha_de_hoy, ag.before_model_callback, rol)


class BarbacoaDesdeExteriorTest(_ConBigQueryFalso):
    """La barbacoa sale de OV_Exterior (obra, portátil, tipo, gas, plancha);
    los códigos de la ficha, que discrepaban en 88 villas, ya no se usan."""

    TIPOS = {
        "tiene_barbacoa": "BOOLEAN", "tiene_barbacoa_obra": "BOOLEAN",
        "tiene_barbacoa_portatil": "BOOLEAN", "tipo_barbacoa_portatil": "STRING",
        "gas_barbacoa": "STRING", "barbacoa_plancha": "STRING",
    }

    def setUp(self):
        super().setUp()
        tipos = patch.object(agent, "_tipos_columnas_villa", return_value=self.TIPOS)
        tipos.start()
        self.addCleanup(tipos.stop)

    def test_la_ficha_muestra_los_datos_de_exterior(self):
        exterior = agent._SECCIONES_FICHA["exterior"]
        for columna in self.TIPOS:
            self.assertIn(columna, exterior, columna)

    def test_ya_no_usa_los_codigos_de_la_ficha(self):
        todas = {c for cols in agent._SECCIONES_FICHA.values() for c in cols}
        self.assertNotIn("tipo_barbacoa_codigo", todas)
        self.assertNotIn("barbacoa_portatil_codigo", todas)

    def test_con_barbacoa_escrito_de_varias_formas(self):
        for escrito in ("barbacoa", "Barbacoa", "bbq", "tiene_barbacoa"):
            agent.buscar_propiedades(caracteristicas=[escrito])
            self.assertIn("v.tiene_barbacoa = TRUE", self._sql(), escrito)

    def test_de_obra_o_portatil(self):
        agent.buscar_propiedades(caracteristicas=["barbacoa de obra"])
        self.assertIn("v.tiene_barbacoa_obra = TRUE", self._sql())
        agent.buscar_propiedades(caracteristicas=["barbacoa portátil"])
        self.assertIn("v.tiene_barbacoa_portatil = TRUE", self._sql())

    def test_portatil_de_gas_por_su_tipo(self):
        agent.buscar_propiedades(caracteristicas=["tipo_barbacoa_portatil = gas"])
        self.assertIn("LOWER(COALESCE(v.tipo_barbacoa_portatil, '')) LIKE", self._sql())
        self.assertEqual("%gas%", self._params()["ficha_0_0"])

    def test_la_disponibilidad_tambien_filtra_por_barbacoa(self):
        agent.consultar_disponibilidad(fecha_desde="2099-08-10", fecha_hasta="2099-08-17",
                                       caracteristicas=["barbacoa"])
        self.assertIn("v.tiene_barbacoa = TRUE", self._sql())

    def test_ya_no_dice_que_el_dato_no_es_fiable(self):
        import re
        aviso = re.compile(r"barbacoa fiable|fiable de barbacoa")
        for herramienta in (agent.buscar_propiedades, agent.consultar_disponibilidad):
            self.assertIsNone(aviso.search(herramienta.__doc__ or ""), herramienta.__name__)
        for rol in ("interno", "admin", "cliente"):
            self.assertIsNone(aviso.search(agent.AGENTS[rol].instruction), rol)


class FiltroPorCualquierDatoDeFichaTest(_ConBigQueryFalso):
    """El usuario puede filtrar por cualquier dato de la ficha, no solo por los
    que tienen parámetro propio."""

    TIPOS = {
        "tiene_pingpong": "BOOLEAN", "tiene_sauna": "BOOLEAN",
        "tiene_ascensor": "BOOLEAN", "tiene_pista_tenis": "BOOLEAN",
        "num_mosquiteras": "INTEGER", "distancia_supermercado_m": "INTEGER",
        "tipo_cafetera_codigo": "STRING", "codigo_alarma_desactivacion": "STRING",
        "fianza": "FLOAT",
    }

    def setUp(self):
        super().setUp()
        secciones = {
            "ocio": ["tiene_pingpong", "tiene_sauna", "tiene_pista_tenis"],
            "equipamiento": ["num_mosquiteras", "tiene_ascensor"],
            "distancias": ["distancia_supermercado_m"],
            "cocina": ["tipo_cafetera_codigo"],
            "acceso_seguridad": ["codigo_alarma_desactivacion"],
            "comercial": ["fianza"],
        }
        for nombre, valor in (("_SECCIONES_FICHA", secciones),
                              ("_FICHA_BASICA", [])):
            parche = patch.object(agent, nombre, valor)
            parche.start()
            self.addCleanup(parche.stop)
        tipos = patch.object(agent, "_tipos_columnas_villa", return_value=self.TIPOS)
        tipos.start()
        self.addCleanup(tipos.stop)

    def test_un_dato_si_no_escrito_de_cualquier_forma(self):
        for escrito in ("pingpong", "ping pong", "Ping-Pong", "tiene_pingpong"):
            agent.buscar_propiedades(caracteristicas=[escrito])
            self.assertIn("v.tiene_pingpong = TRUE", self._sql(), escrito)

    def test_negado(self):
        agent.buscar_propiedades(caracteristicas=["sin ascensor"])
        self.assertIn("COALESCE(v.tiene_ascensor, FALSE) = FALSE", self._sql())

    def test_numeros_con_comparacion_como_parametro(self):
        agent.buscar_propiedades(caracteristicas=["num_mosquiteras >= 2"])
        self.assertIn("v.num_mosquiteras >= @ficha_0_0", self._sql())
        self.assertEqual(2.0, self._params()["ficha_0_0"])

    def test_una_distancia_cero_es_sin_dato(self):
        agent.buscar_propiedades(caracteristicas=["distancia_supermercado_m <= 500"])
        self.assertIn("v.distancia_supermercado_m > 0", self._sql())

    def test_texto_por_parametro_y_sin_inyeccion(self):
        agent.buscar_propiedades(caracteristicas=["tipo_cafetera_codigo = x' OR 1=1 --"])
        sql = self._sql()
        self.assertIn("LOWER(COALESCE(v.tipo_cafetera_codigo, '')) LIKE LOWER(@ficha_0_0)", sql)
        self.assertNotIn("OR 1=1", sql)

    def test_una_o_entre_caracteristicas(self):
        for escrito in ("sauna | pingpong", "sauna o ping pong"):
            agent.buscar_propiedades(caracteristicas=[escrito, "num_mosquiteras >= 1"])
            sql = self._sql()
            self.assertIn("(v.tiene_sauna = TRUE OR v.tiene_pingpong = TRUE)", sql, escrito)
            self.assertIn("v.num_mosquiteras >= @ficha_1_0", sql, escrito)

    def test_un_dato_que_no_existe_sugiere_los_parecidos_sin_consultar(self):
        r = agent.buscar_propiedades(caracteristicas=["mosquitera"])
        self.assertIn("num_mosquiteras", r["error"])
        self.bq.query.assert_not_called()

    def test_no_se_filtra_por_datos_de_acceso_ni_comerciales(self):
        for dato in ("codigo_alarma_desactivacion = 1234", "fianza > 100"):
            r = agent.buscar_propiedades(caracteristicas=[dato])
            self.assertIn("error", r, dato)
        self.bq.query.assert_not_called()

    def test_tambien_en_disponibilidad(self):
        agent.consultar_disponibilidad(fecha_desde="2099-10-03",
                                       fecha_hasta="2099-10-10",
                                       caracteristicas=["pingpong", "sauna"])
        sql = self._sql()
        self.assertIn("v.tiene_pingpong = TRUE", sql)
        self.assertIn("v.tiene_sauna = TRUE", sql)

    def test_una_villa_concreta_se_mira_en_su_ficha(self):
        self.assertIn("No digas que no\n  tienes el dato sin haber mirado la ficha",
                      agent.INSTRUCTION_INTERNO)


class BuscarOfertasTest(_ConBigQueryFalso):
    """Disponibilidad + precio + características en una sola llamada."""

    def _ofertas(self, **kw):
        base = dict(fecha_desde="2099-10-03", fecha_hasta="2099-10-10")
        return agent.buscar_ofertas(**{**base, **kw})

    def test_misma_regla_de_libre_que_la_disponibilidad(self):
        self._ofertas()
        ofertas = self._sql()
        agent.consultar_disponibilidad(fecha_desde="2099-10-03", fecha_hasta="2099-10-10")
        disponibilidad = self._sql()
        self.assertIn(agent._sql_villa_libre().strip(), ofertas)
        self.assertIn(agent._sql_villa_libre().strip(), disponibilidad)

    def test_suma_la_tarifa_de_venta_de_cada_noche_sin_la_de_salida(self):
        self._ofertas()
        sql = self._sql()
        self.assertIn("t.nombre = 'PrecioVilla'", sql)
        self.assertIn("o.fecha < @fecha_hasta", sql)
        self.assertEqual(7, self._params()["noches"])

    def test_la_larga_estancia_solo_si_llega_a_su_minimo(self):
        self._ofertas()
        self.assertIn("t.noches_larga = @noches AND @noches >= t.minimo_larga", self._sql())

    def test_sin_compra_de_todas_las_noches_no_hay_margen(self):
        self._ofertas()
        self.assertIn("t.noches_con_compra = @noches", self._sql())

    def test_el_presupuesto_exige_precio_de_todas_las_noches(self):
        self._ofertas(presupuesto_max=4000)
        primera = self.bq.query.call_args_list[0]
        self.assertIn("precio_completo AND precio_total <= @presupuesto_max", primera[0][0])
        valores = {p.name: p.value for p in primera[1]["job_config"].query_parameters}
        self.assertEqual(4000.0, valores["presupuesto_max"])

    def test_si_nada_entra_en_el_presupuesto_ofrece_las_mas_baratas(self):
        romeo = _Fila(nombre="ROMEO", precio_total=5432.0, margen_total=None,
                      precio_completo=True, larga_estancia=False,
                      total_resultados=1, total_libres=1)
        self.bq.query.return_value.result.side_effect = [[], [romeo]]
        r = self._ofertas(presupuesto_max=4000)
        self.assertEqual(0, r["total"])
        self.assertEqual("ROMEO", r["fuera_de_presupuesto"][0]["nombre"])
        self.assertIn("ROMEO por 5432.0 €", r["aviso"])
        self.assertIn("4.000 €", r["aviso"])
        sin_presupuesto = self.bq.query.call_args_list[1][0][0]
        self.assertNotIn("@presupuesto_max", sin_presupuesto)

    def test_ordena_por_precio_o_por_margen(self):
        self._ofertas()
        self.assertIn("ORDER BY precio_total IS NULL, precio_total, nombre", self._sql())
        self._ofertas(orden="margen")
        self.assertIn("ORDER BY margen_total IS NULL, margen_total DESC", self._sql())

    def test_un_orden_desconocido_es_error_sin_consultar(self):
        r = self._ofertas(orden="barato")
        self.assertIn("error", r)
        self.bq.query.assert_not_called()

    def test_fechas_pasadas_o_al_reves_son_error_sin_consultar(self):
        self.assertIn("pasada", agent.buscar_ofertas("2000-01-01", "2000-01-08")["error"])
        self.assertIn("posterior", self._ofertas(fecha_hasta="2099-10-01")["error"])
        self.bq.query.assert_not_called()

    def test_filtra_como_la_busqueda(self):
        self._ofertas(ubicacion="Moraira", capacidad_min=6, admite_animales=True,
                      direccion="Cumbre del Sol")
        sql = self._sql()
        self.assertIn("LOWER(v.pueblo_cercano) LIKE LOWER(@ubicacion)", sql)
        self.assertIn("v.capacidad_pax >= @capacidad_min", sql)
        self.assertIn("v.admite_animales = TRUE", sql)
        self.assertIn("LOWER(v.direccion) LIKE @direccion_0", sql)

    def test_devuelve_totales_y_periodo(self):
        self.bq.query.return_value.result.return_value = [_Fila(
            nombre="WATERFRONT", precio_total=1287.0, margen_total=454.0,
            precio_completo=True, larga_estancia=False, total_resultados=3,
            total_libres=5)]
        r = self._ofertas()
        self.assertEqual(3, r["total"])
        self.assertEqual(5, r["total_libres"])
        self.assertEqual({"entrada": "2099-10-03", "salida": "2099-10-10", "noches": 7},
                         r["periodo"])
        self.assertNotIn("total_resultados", r["matches"][0])


class AlternativasVillaTest(_ConBigQueryFalso):
    """Villa pedida, otras fechas de la misma villa y villas parecidas."""

    def test_nombre_ambiguo_pide_cual(self):
        self.bq.query.return_value.result.return_value = [
            _Fila(villa_id="1", villa_nombre="ADORA"),
            _Fila(villa_id="2", villa_nombre="ADORABLE")]
        r = agent.alternativas_villa("ADO", "2099-10-03", "2099-10-10")
        self.assertEqual(["ADORA", "ADORABLE"], r["villas_coincidentes"])

    def test_villa_que_no_existe(self):
        r = agent.alternativas_villa("NOEXISTE", "2099-10-03", "2099-10-10")
        self.assertIn("No hay ninguna villa", r["error"])

    def test_consultas_de_la_villa_parecidas_y_otras_fechas(self):
        villa = _Fila(villa_id="v1", villa_nombre="ATALAYA", zona="Moraira",
                      pueblo_cercano="Moraira", capacidad_pax=8,
                      tiene_piscina_privada=True)
        self.bq.query.return_value.result.side_effect = (
            lambda *a, **k: [villa] if self.bq.query.call_count == 1 else [])
        r = agent.alternativas_villa("Atalaya", "2099-10-03", "2099-10-10")
        self.assertIsNone(r.get("error"))
        self.assertFalse(r["villa_pedida"]["libre"])
        consultas = [c[0][0] for c in self.bq.query.call_args_list]
        parecidas = next(q for q in consultas if "v.villa_id != @villa_id" in q)
        self.assertIn("v.capacidad_pax >= @capacidad_min", parecidas)
        self.assertIn("LOWER(v.zona) = LOWER(@zona_ref)", parecidas)
        self.assertIn("v.tiene_piscina_privada = TRUE", parecidas)
        self.assertIn("ABS(precio_total - COALESCE(", parecidas)
        otras = next(q for q in consultas if "GENERATE_DATE_ARRAY" in q)
        self.assertIn(agent._sql_villa_libre("c.entrada", "c.salida").strip(), otras)


class OfertasSoloParaGestionTest(unittest.TestCase):

    def test_el_cliente_no_ve_precios_de_compra_ni_margenes(self):
        nombres = {t.__name__ for t in agent.agent_cliente.tools}
        self.assertNotIn("buscar_ofertas", nombres)
        self.assertNotIn("alternativas_villa", nombres)
        for rol in ("interno", "admin"):
            nombres = {t.__name__ for t in agent.AGENTS[rol].tools}
            self.assertTrue({"buscar_ofertas", "alternativas_villa"} <= nombres, rol)


class InstruccionesMapasTest(unittest.TestCase):

    def test_el_agente_sabe_que_la_app_pinta_mapas_y_graficos(self):
        for instruccion in (agent.INSTRUCTION_CLIENTE, agent.INSTRUCTION_INTERNO,
                            agent.INSTRUCTION_ADMIN):
            self.assertIn("NUNCA digas que no puedes hacer\n  mapas o gráficos", instruccion)


class ArgumentosDelModeloTest(unittest.TestCase):
    """El modelo a veces manda una lista donde la herramienta espera texto."""

    def setUp(self):
        from google.adk.tools import FunctionTool
        self.detalle = FunctionTool(agent.obtener_detalle_propiedad)

    def test_una_lista_de_un_elemento_se_convierte_en_texto(self):
        args = {"nombre": ["Villa Atalaya"]}
        self.assertIsNone(agent.normalizar_argumentos(self.detalle, args, None))
        self.assertEqual("Atalaya", args["nombre"])

    def test_varios_valores_piden_una_llamada_por_cada_uno(self):
        args = {"nombre": ["ATALAYA", "ROMEO"]}
        respuesta = agent.normalizar_argumentos(self.detalle, args, None)
        self.assertIn("una vez por cada uno", respuesta["error"])
        self.assertIn("ATALAYA, ROMEO", respuesta["error"])

    def test_los_parametros_que_si_son_listas_no_se_tocan(self):
        args = {"nombre": "ATALAYA", "secciones": ["piscina", "vistas"]}
        self.assertIsNone(agent.normalizar_argumentos(self.detalle, args, None))
        self.assertEqual(["piscina", "vistas"], args["secciones"])

    def test_un_numero_en_un_parametro_de_texto_pasa_a_texto(self):
        args = {"nombre": 25}
        agent.normalizar_argumentos(self.detalle, args, None)
        self.assertEqual("25", args["nombre"])

    def test_quita_villa_delante_del_nombre(self):
        from google.adk.tools import FunctionTool
        args = {"villa_nombre": "Villa Atalaya"}
        agent.normalizar_argumentos(FunctionTool(agent.consultar_precios), args, None)
        self.assertEqual("Atalaya", args["villa_nombre"])

    def test_un_nombre_que_solo_es_villa_no_se_vacia(self):
        from google.adk.tools import FunctionTool
        args = {"villa_nombre": "Villa"}
        agent.normalizar_argumentos(FunctionTool(agent.consultar_precios), args, None)
        self.assertEqual("Villa", args["villa_nombre"])

    def test_todos_los_agentes_llevan_las_salvaguardas(self):
        for rol, ag in agent.AGENTS.items():
            self.assertIs(agent.normalizar_argumentos, ag.before_tool_callback, rol)
            self.assertIs(agent.error_de_herramienta, ag.on_tool_error_callback, rol)


class ErrorDeHerramientaTest(unittest.TestCase):
    """Un fallo dentro de una herramienta no debe tumbar la respuesta."""

    def test_el_error_vuelve_al_modelo_como_dato(self):
        tool = Mock()
        tool.name = "consultar_reservas"
        respuesta = agent.error_de_herramienta(
            tool, {}, None, AttributeError("'list' object has no attribute 'strip'"))
        self.assertIn("consultar_reservas", respuesta["error"])
        self.assertIn("AttributeError", respuesta["error"])

    def test_el_turno_sigue_con_una_lista_en_un_parametro_de_texto(self):
        """De punta a punta con ADK: la llamada que antes fallaba ya no corta."""
        import asyncio
        from google.adk.models import BaseLlm, LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        recibido = {}

        def ficha(nombre: str) -> dict:
            recibido["nombre"] = nombre
            return {"matches": [{"nombre": nombre.strip()}], "count": 1}

        class ModeloFalso(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                ya_llamo = any(
                    p.function_response for c in llm_request.contents for p in c.parts or [])
                if ya_llamo:
                    parte = types.Part(text="Villa Atalaya está en Jávea.")
                else:
                    parte = types.Part(function_call=types.FunctionCall(
                        name="ficha", args={"nombre": ["Villa Atalaya"]}))
                yield LlmResponse(content=types.Content(role="model", parts=[parte]))

        prueba = agent.Agent(
            name="prueba", model=ModeloFalso(model="falso"), instruction="",
            tools=[ficha],
            before_tool_callback=agent.normalizar_argumentos,
            on_tool_error_callback=agent.error_de_herramienta,
        )

        async def conversar():
            sesiones = InMemorySessionService()
            await sesiones.create_session(app_name="t", user_id="u", session_id="s")
            runner = Runner(agent=prueba, app_name="t", session_service=sesiones)
            textos = []
            async for evento in runner.run_async(
                user_id="u", session_id="s",
                new_message=types.Content(role="user", parts=[types.Part(text="hola")]),
            ):
                if evento.is_final_response() and evento.content:
                    textos += [p.text for p in evento.content.parts if p.text]
            return "".join(textos)

        self.assertEqual("Villa Atalaya está en Jávea.", asyncio.run(conversar()))
        self.assertEqual("Atalaya", recibido["nombre"])

if __name__ == "__main__":
    unittest.main()
