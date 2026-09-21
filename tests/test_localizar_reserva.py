"""Localizar una reserva por número, titular, villa o fechas, y ver su detalle.

El número es el localizador de Etendo ("2026_3079"), que Reservas dice de
muchas formas; el titular se guarda como "APELLIDOS, Nombre", con tildes y
espacios de más.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class _Fila(dict):
    """Imita una Row de BigQuery: se comporta como dict con .items()."""


class _BQ:
    """BigQuery falso: guarda cada consulta y responde según su contenido."""

    def __init__(self, respuestas=None, fallos=()):
        self.consultas = []
        self.respuestas = respuestas or {}
        self.fallos = fallos

    def query(self, sql, job_config=None):
        self.consultas.append((sql, job_config))
        for clave in self.fallos:
            if clave in sql:
                raise RuntimeError(f"{clave} no disponible")
        filas = next((f for clave, f in self.respuestas.items() if clave in sql), [])
        return Mock(**{"result.return_value": [_Fila(x) for x in filas]})

    def params(self, i=-1):
        return {p.name: p.value for p in self.consultas[i][1].query_parameters}


class BuscarPorNumeroTest(unittest.TestCase):

    def _buscar(self, **kwargs):
        bq = _BQ()
        with patch.object(agent, "_bq", bq):
            agent.consultar_reservas(**kwargs)
        return bq.consultas[-1][0], bq.params()

    def test_el_numero_completo_en_cualquier_formato(self):
        for escrito in ("2026_3079", "2026/3079", "2026-3079", " 2026 3079 "):
            sql, params = self._buscar(localizador=escrito)
            self.assertIn("r.localizador = @localizador", sql, escrito)
            self.assertEqual("2026_3079", params["localizador"], escrito)

    def test_solo_el_numero_vale_para_cualquier_ano(self):
        sql, params = self._buscar(localizador="3079")
        self.assertIn("REGEXP_CONTAINS(r.localizador, @localizador)", sql)
        self.assertEqual(r"(^|_)3079$", params["localizador"])

    def test_por_numero_salen_todos_los_estados(self):
        # La reserva buscada puede estar perdida o anulada.
        sql, _ = self._buscar(localizador="2026_3079")
        self.assertNotIn("'PERDIDA'", sql)


class BuscarPorTitularTest(unittest.TestCase):

    def _buscar(self, **kwargs):
        bq = _BQ()
        with patch.object(agent, "_bq", bq):
            agent.consultar_reservas(**kwargs)
        return bq.consultas[-1][0], bq.params()

    def test_cada_palabra_sin_tildes_y_en_cualquier_orden(self):
        sql, params = self._buscar(titular="Verónica García")
        self.assertIn("NORMALIZE", sql)
        self.assertEqual({"%veronica%", "%garcia%"},
                         {v for k, v in params.items() if k.startswith("titular_")})

    def test_ignora_la_coma_y_los_espacios(self):
        _, params = self._buscar(titular="  García Tomás ,  Verónica ")
        self.assertEqual({"%garcia%", "%tomas%", "%veronica%"},
                         {v for k, v in params.items() if k.startswith("titular_")})

    def test_se_combina_con_villa_y_fechas(self):
        sql, params = self._buscar(titular="Farley", villa_nombre="MARACALA",
                                   fecha_desde="2026-07-01", salida_hasta="2026-08-31")
        for trozo in ("r.villa_nombre", "r.fecha_entrada >= @fecha_desde",
                      "r.fecha_salida <= @salida_hasta"):
            self.assertIn(trozo, sql)


RESERVA = {
    "reserva_id": "R1", "localizador": "2026_3274", "villa_nombre": "MARACALA",
    "cliente_nombre": "Farley, Olivia", "fecha_entrada": "2026-07-25",
    "fecha_salida": "2026-08-15", "noches": 21, "adultos": 6, "ninos": 2,
    "importe_total": 18000.0, "titular_email": "olivia@example.com",
}


class DetalleReservaTest(unittest.TestCase):

    def _detalle(self, respuestas, fallos=(), localizador="2026_3274"):
        bq = _BQ(respuestas, fallos)
        with patch.object(agent, "_bq", bq):
            return agent.detalle_reserva(localizador), bq

    def test_junta_reserva_titular_y_pagos(self):
        pagos = [
            {"fecha_vencimiento": "2026-05-01", "importe_previsto": 5400.0,
             "importe_pagado": 5400.0, "importe_pendiente": 0.0},
            {"fecha_vencimiento": "2026-07-10", "importe_previsto": 12600.0,
             "importe_pagado": 0.0, "importe_pendiente": 12600.0},
        ]
        r, bq = self._detalle({"stg_etendo_Reserva": [RESERVA],
                               "stg_etendo_PlanFacturacion": pagos})
        self.assertEqual("2026_3274", r["reserva"]["localizador"])
        self.assertEqual("olivia@example.com", r["reserva"]["titular_email"])
        self.assertEqual(2, len(r["pagos"]))
        self.assertEqual({"previsto": 18000.0, "pagado": 5400.0, "pendiente": 12600.0},
                         r["resumen_pagos"])
        self.assertNotIn("reserva_id", r["reserva"])

    def test_el_titular_sale_de_terceros(self):
        _, bq = self._detalle({"stg_etendo_Reserva": [RESERVA]})
        sql = bq.consultas[0][0]
        self.assertIn("stg_etendo_Tercero", sql)
        self.assertIn("r.cliente_id", sql)

    def test_varias_coincidencias_piden_elegir(self):
        otra = dict(RESERVA, reserva_id="R2", localizador="2025_3274", villa_nombre="DIVA")
        r, bq = self._detalle({"stg_etendo_Reserva": [RESERVA, otra]}, localizador="3274")
        self.assertIn("error", r)
        self.assertEqual({"2026_3274", "2025_3274"},
                         {c["localizador"] for c in r["candidatas"]})
        self.assertEqual(1, len(bq.consultas))   # no pide pagos de ninguna

    def test_una_reserva_que_no_existe(self):
        r, _ = self._detalle({"stg_etendo_Reserva": []}, localizador="2026_999999")
        self.assertIn("error", r)
        self.assertIn("2026_999999", r["error"])

    def test_si_fallan_los_pagos_la_reserva_sale_igual(self):
        with self.assertLogs("agente-villas", level="WARNING"):
            r, _ = self._detalle({"stg_etendo_Reserva": [RESERVA]},
                                 fallos=("stg_etendo_PlanFacturacion",))
        self.assertEqual("MARACALA", r["reserva"]["villa_nombre"])
        self.assertFalse(r["pagos_disponibles"])

    def test_los_pagos_tienen_margen_de_lectura(self):
        # La tabla de pagos pesa más que el límite general de 50 MB.
        _, bq = self._detalle({"stg_etendo_Reserva": [RESERVA]})
        config = next(c for s, c in bq.consultas if "stg_etendo_PlanFacturacion" in s)
        self.assertGreater(config.maximum_bytes_billed, agent._BILLING_CAP)


class QuienPuedeVerLasReservasTest(unittest.TestCase):

    def test_solo_interno_y_admin(self):
        for rol, puede in (("cliente", False), ("interno", True), ("admin", True)):
            nombres = {t.__name__ for t in agent.AGENTS[rol].tools}
            self.assertEqual(puede, "detalle_reserva" in nombres, rol)

    def test_las_instrucciones_explican_como_localizarla(self):
        for rol in ("interno", "admin"):
            instruccion = agent.AGENTS[rol].instruction
            self.assertIn("detalle_reserva", instruccion, rol)
            self.assertIn("titular", instruccion, rol)


if __name__ == "__main__":
    unittest.main()
