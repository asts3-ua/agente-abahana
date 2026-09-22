"""Cuándo se actualizó cada dato de una respuesta.

Lo que cuenta es el último cambio en Etendo, no solo cuándo se cargó: con la
copia de pre-control las cargas funcionan cada noche pero el último cambio de
reservas puede ser de hace días, y "actualizado hoy" daría falsa confianza.
"""

import datetime
import unittest
from zoneinfo import ZoneInfo

import frescura

MADRID = ZoneInfo("Europe/Madrid")
AHORA = datetime.datetime(2026, 9, 22, 13, 30, tzinfo=MADRID)
UTC = datetime.timezone.utc


def _dt(y, mo, d, h, mi, tz=UTC):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz)


DATOS = {
    # 19:01 UTC del 16 = 21:01 en Madrid
    "reservas": {"ultimo_cambio": _dt(2026, 9, 16, 19, 1), "cargado": _dt(2026, 9, 22, 11, 11)},
    "villas": {"ultimo_cambio": _dt(2026, 9, 9, 9, 42), "cargado": _dt(2026, 9, 22, 11, 11)},
    "disponibilidad": {"ultimo_cambio": _dt(2026, 9, 22, 9, 0), "cargado": _dt(2026, 9, 22, 11, 11)},
    "precios": {"ultimo_cambio": _dt(2026, 8, 25, 7, 40), "cargado": _dt(2026, 9, 21, 3, 5)},
}


class QueDatosUsaCadaHerramientaTest(unittest.TestCase):

    def test_reservas(self):
        self.assertEqual(["reservas"], frescura.dominios([("detalle_reserva", {}, {})]))

    def test_ofertas_usan_villas_disponibilidad_y_precios_sin_repetir(self):
        self.assertEqual(
            ["villas", "disponibilidad", "precios"],
            frescura.dominios([("buscar_ofertas", {}, {}), ("buscar_propiedades", {}, {})]))

    def test_la_web_no_es_un_dato_de_etendo(self):
        self.assertEqual([], frescura.dominios([("buscar_internet", {}, {})]))
        self.assertTrue(frescura.uso_web([("consultar_web", {}, {})]))


class LineasTest(unittest.TestCase):

    def test_ultimo_cambio_en_hora_de_madrid_y_carga_de_hoy(self):
        lineas = frescura.lineas(["reservas"], DATOS, AHORA)
        self.assertEqual(1, len(lineas))
        self.assertIn("Reservas: último cambio en Etendo el mié 16 sep 21:01", lineas[0])
        self.assertIn("cargadas hoy a las 13:11", lineas[0])

    def test_avisa_si_las_reservas_llevan_mas_de_un_dia_sin_cambios(self):
        self.assertIn("puede no estar al día", frescura.lineas(["reservas"], DATOS, AHORA)[0])

    def test_no_avisa_si_hay_cambios_recientes(self):
        linea = frescura.lineas(["disponibilidad"], DATOS, AHORA)[0]
        self.assertIn("último cambio en Etendo hoy a las 11:00", linea)
        self.assertNotIn("puede no estar al día", linea)

    def test_en_villas_y_precios_un_cambio_antiguo_es_normal(self):
        for linea in frescura.lineas(["villas", "precios"], DATOS, AHORA):
            self.assertNotIn("puede no estar al día", linea)

    def test_ayer(self):
        linea = frescura.lineas(["precios"], DATOS, AHORA)[0]
        self.assertIn("cargados ayer a las 05:05", linea)

    def test_web_consultada_ahora(self):
        self.assertEqual(["Web e internet: consultado hoy a las 13:30"],
                         frescura.lineas([], DATOS, AHORA, web=True))

    def test_sin_datos_de_un_dominio_no_inventa(self):
        self.assertEqual([], frescura.lineas(["reservas"], {}, AHORA))


class ConsultaTest(unittest.TestCase):

    def test_una_sola_consulta_que_lee_solo_la_columna_de_fecha(self):
        from unittest.mock import Mock
        bq = Mock()
        bq.query.return_value.result.return_value = [
            Mock(dominio="reservas", ultimo_cambio=_dt(2026, 9, 16, 19, 1))]
        bq.get_table.return_value = Mock(modified=_dt(2026, 9, 22, 11, 11))
        datos = frescura.consultar(bq)
        sql = bq.query.call_args[0][0]
        self.assertEqual(1, bq.query.call_count)
        self.assertIn("MAX(fecha_actualizacion)", sql)
        self.assertNotIn("SELECT *", sql)
        self.assertEqual(_dt(2026, 9, 16, 19, 1), datos["reservas"]["ultimo_cambio"])
        self.assertEqual(_dt(2026, 9, 22, 11, 11), datos["reservas"]["cargado"])


class DespliegueTest(unittest.TestCase):

    def test_la_imagen_incluye_el_modulo(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile")) as f:
            self.assertIn("COPY frescura.py .", f.read())


if __name__ == "__main__":
    unittest.main()
