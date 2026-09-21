"""Tests del resumen de filtros que ve el usuario bajo cada respuesta."""

import unittest

import filtros


class DescribirLlamadaTest(unittest.TestCase):

    def test_el_ejemplo_del_equipo(self):
        linea = filtros.describir_llamada("consultar_disponibilidad", {
            "ubicacion": "Moraira", "capacidad_min": 6,
            "fecha_desde": "2026-10-03", "fecha_hasta": "2026-10-10",
            "admite_animales": True,
        })
        self.assertEqual(
            "Disponibilidad: Moraira · del 3 al 10 oct 2026 · 6+ personas · admite mascotas",
            linea)

    def test_las_fechas_de_reservas_dicen_si_son_de_entrada_o_de_salida(self):
        self.assertEqual(
            "Reservas: entrada el 21 sep 2026",
            filtros.describir_llamada("consultar_reservas", {
                "fecha_desde": "2026-09-21", "fecha_hasta": "2026-09-21"}))
        self.assertEqual(
            "Reservas: salida el 21 sep 2026",
            filtros.describir_llamada("consultar_reservas", {
                "salida_desde": "2026-09-21", "salida_hasta": "2026-09-21"}))

    def test_un_solo_extremo_de_fecha_se_ve(self):
        # Fue un error real: con solo salida_hasta salían 37.747 reservas.
        self.assertEqual(
            "Reservas: salida hasta el 21 sep 2026",
            filtros.describir_llamada("consultar_reservas", {"salida_hasta": "2026-09-21"}))

    def test_rango_entre_meses_y_entre_anios(self):
        self.assertIn("del 28 sep al 5 oct 2026", filtros.describir_llamada(
            "consultar_disponibilidad",
            {"fecha_desde": "2026-09-28", "fecha_hasta": "2026-10-05"}))
        self.assertIn("del 28 dic 2026 al 3 ene 2027", filtros.describir_llamada(
            "consultar_disponibilidad",
            {"fecha_desde": "2026-12-28", "fecha_hasta": "2027-01-03"}))

    def test_ocupacion_anulaciones_y_estados(self):
        self.assertEqual("Reservas: ocupadas el 21 sep 2026", filtros.describir_llamada(
            "consultar_reservas", {"activa_en": "2026-09-21"}))
        self.assertEqual(
            "Reservas: anuladas desde el 22 ago 2026 · canceladas",
            filtros.describir_llamada("consultar_reservas", {
                "anulada_desde": "2026-08-22", "estado_reserva": "CA"}))

    def test_equipamiento_exigido_y_excluido(self):
        self.assertEqual("Villas: Calpe · sin piscina privada · jacuzzi",
                         filtros.describir_llamada("buscar_propiedades", {
                             "ubicacion": "Calpe", "piscina": False, "jacuzzi": True}))

    def test_las_condiciones_de_ficha_se_leen_bien(self):
        self.assertEqual(
            "Villas: Moraira · ping pong · sin ascensor · num mosquiteras ≥ 2",
            filtros.describir_llamada("buscar_propiedades", {
                "ubicacion": "Moraira",
                "caracteristicas": ["tiene_pingpong", "sin ascensor", "num_mosquiteras>=2"]}))

    def test_ofertas_con_presupuesto_y_orden(self):
        self.assertEqual(
            "Ofertas: Moraira · del 3 al 10 oct 2026 · 6+ personas · hasta 4.000 € · orden: margen",
            filtros.describir_llamada("buscar_ofertas", {
                "ubicacion": "Moraira", "fecha_desde": "2026-10-03",
                "fecha_hasta": "2026-10-10", "capacidad_min": 6,
                "presupuesto_max": 4000, "orden": "margen", "limite": 10}))

    def test_una_o_entre_caracteristicas_se_lee_como_o(self):
        self.assertEqual("Villas: sauna o jacuzzi", filtros.describir_llamada(
            "buscar_propiedades", {"caracteristicas": ["tiene_sauna | tiene_jacuzzi"]}))

    def test_villa_concreta_y_secciones(self):
        self.assertEqual(
            "Ficha: villa ATALAYA · secciones: piscina, vistas",
            filtros.describir_llamada("obtener_detalle_propiedad", {
                "nombre": "Atalaya", "secciones": ["piscina", "vistas"]}))

    def test_lo_desconocido_se_ensena_tal_cual(self):
        self.assertIn("parametro_nuevo: 3", filtros.describir_llamada(
            "buscar_propiedades", {"parametro_nuevo": 3}))

    def test_sin_filtros_se_dice(self):
        self.assertEqual("Villas: sin filtros",
                         filtros.describir_llamada("listar_propiedades", {}))

    def test_limite_y_vacios_no_son_filtros(self):
        self.assertEqual("Villas: Calpe", filtros.describir_llamada(
            "buscar_propiedades", {"ubicacion": "Calpe", "limite": 20, "zona": None}))

    def test_las_herramientas_que_no_buscan_no_salen(self):
        for nombre in ("obtener_fecha_hora_actual", "buscar_internet", "consultar_web"):
            self.assertIsNone(filtros.describir_llamada(nombre, {"x": 1}), nombre)

    def test_una_sql_a_medida_se_avisa_sin_enseñar_la_consulta(self):
        self.assertEqual("Consulta SQL a medida", filtros.describir_llamada(
            "ejecutar_sql", {"query": "SELECT 1"}))


class DescribirTurnoTest(unittest.TestCase):

    def test_una_linea_por_busqueda_sin_repetir_y_en_orden(self):
        lineas = filtros.describir([
            ("obtener_fecha_hora_actual", {}, {}),
            ("obtener_detalle_propiedad", {"nombre": "Atalaya"}, {}),
            ("consultar_precios", {"villa_nombre": "Atalaya"}, {}),
            ("obtener_detalle_propiedad", {"nombre": "Atalaya"}, {}),
        ])
        self.assertEqual(["Ficha: villa ATALAYA", "Precios: villa ATALAYA"], lineas)


if __name__ == "__main__":
    unittest.main()
