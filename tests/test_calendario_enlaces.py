"""Desde el calendario a la reserva: cada día ocupado por una reserva abre esa
reserva en Etendo.

El `reserva_id` de la ocupación no sirve: guarda códigos del canal
("SYNC_PREVIO", números de agencia). La reserva se busca por villa y fechas.
"""

import datetime
import re
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import enlaces
    import visualizaciones as viz

RESERVA = "D93EBCFB44394A03BA5219CC26A05171"
VILLA = "BFC7BA9C6D9448ADBFB7219AAF09F502"


class TramosConReservaTest(unittest.TestCase):

    TRAMOS = [
        {"desde": "2026-10-01", "hasta": "2026-10-03", "tipo_ocupacion": "Libre", "noches": 3},
        {"desde": "2026-10-04", "hasta": "2026-10-10", "tipo_ocupacion": "Reserva Agencia", "noches": 7},
        {"desde": "2026-10-11", "hasta": "2026-10-12", "tipo_ocupacion": "Reserva Propietario", "noches": 2},
    ]
    RESERVAS = [
        {"reserva_id": RESERVA, "localizador": "2026_1741",
         "fecha_entrada": datetime.date(2026, 10, 4), "fecha_salida": datetime.date(2026, 10, 11)},
    ]

    def setUp(self):
        p = patch.object(enlaces, "base_etendo", return_value="https://pre-control.abahanavillas.com")
        p.start()
        self.addCleanup(p.stop)

    def _enlazar(self, reservas=None):
        tramos = [dict(t) for t in self.TRAMOS]
        bq = Mock()
        bq.query.return_value = Mock(**{"result.return_value": reservas if reservas is not None else self.RESERVAS})
        with patch.object(agent, "_bq", bq), patch.object(agent, "_row_to_dict", side_effect=lambda f: dict(f)):
            agent._enlazar_tramos(tramos, VILLA, datetime.date(2026, 10, 1), datetime.date(2026, 10, 12))
        self.sql = bq.query.call_args[0][0] if bq.query.call_args else ""
        return tramos

    def test_las_fechas_pueden_venir_como_texto(self):
        # BigQuery las devuelve en ISO al pasar por _row_to_dict.
        reservas = [{"reserva_id": RESERVA, "localizador": "2026_1741",
                     "fecha_entrada": "2026-10-04", "fecha_salida": "2026-10-11"}]
        self.assertEqual("2026_1741", self._enlazar(reservas)[1]["localizador"])

    def test_el_tramo_ocupado_apunta_a_su_reserva(self):
        tramo = self._enlazar()[1]
        self.assertEqual("2026_1741", tramo["localizador"])
        self.assertIn(RESERVA, tramo["enlace_etendo"])

    def test_los_libres_y_los_que_no_casan_se_quedan_igual(self):
        tramos = self._enlazar()
        self.assertNotIn("enlace_etendo", tramos[0])   # libre
        self.assertNotIn("enlace_etendo", tramos[2])   # propietario, sin reserva que case

    def test_no_cuenta_las_canceladas(self):
        self._enlazar()
        self.assertIn("NOT IN", self.sql)
        self.assertIn("CANCELADA", self.sql)

    def test_si_falla_la_consulta_el_calendario_sigue(self):
        bq = Mock()
        bq.query.side_effect = RuntimeError("caído")
        tramos = [dict(t) for t in self.TRAMOS]
        with patch.object(agent, "_bq", bq), self.assertLogs("agente-villas", level="WARNING"):
            agent._enlazar_tramos(tramos, VILLA, datetime.date(2026, 10, 1), datetime.date(2026, 10, 12))
        self.assertNotIn("enlace_etendo", tramos[1])


class CalendarioConEnlacesTest(unittest.TestCase):

    VIZ = {"tipo": "calendario", "villa": "MAR CORAL", "resumen": {}, "tramos": [
        {"desde": "2026-10-01", "hasta": "2026-10-03", "tipo_ocupacion": "Libre", "noches": 3},
        {"desde": "2026-10-04", "hasta": "2026-10-10", "tipo_ocupacion": "Reserva Agencia", "noches": 7,
         "localizador": "2026_1741",
         "enlace_etendo": "https://pre-control.abahanavillas.com/etendo/#x"},
    ]}

    def _dias(self, html):
        return re.findall(r"<t[dh][^>]*>(?:<a[^>]*href=\"([^\"]*)\"[^>]*>)?(\d+)", html)

    def test_los_dias_de_la_reserva_son_enlaces(self):
        html = viz.calendario_html(self.VIZ)
        enlazados = {n for href, n in self._dias(html) if href}
        self.assertEqual({"4", "5", "6", "7", "8", "9", "10"}, enlazados)

    def test_los_dias_libres_no(self):
        html = viz.calendario_html(self.VIZ)
        libres = {n for href, n in self._dias(html) if not href}
        self.assertIn("1", libres)

    def test_se_abre_en_otra_pestana_y_lo_dice_al_pasar_el_raton(self):
        html = viz.calendario_html(self.VIZ)
        self.assertIn('target="_blank"', html)
        self.assertIn("2026_1741", html)
        self.assertIn("abrir en Etendo", html)

    def test_sin_enlace_el_calendario_es_el_de_siempre(self):
        html = viz.calendario_html({"tipo": "calendario", "tramos": [
            {"desde": "2026-10-04", "hasta": "2026-10-05", "tipo_ocupacion": "Reserva", "noches": 2}]})
        self.assertNotIn("<a ", html)


if __name__ == "__main__":
    unittest.main()
