import datetime
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import agent


class _QueryResult:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class AgentToolsTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(
            2026, 7, 19, 16, 0, tzinfo=ZoneInfo("Europe/Madrid")
        )

    def test_current_datetime_uses_madrid_timezone(self):
        with patch.object(agent, "_ahora_local", return_value=self.now):
            result = agent.obtener_fecha_hora_actual()

        self.assertEqual(result["fecha"], "2026-07-19")
        self.assertEqual(result["hora"], "16:00:00")
        self.assertEqual(result["zona_horaria"], "Europe/Madrid")
        self.assertTrue(result["fecha_hora"].endswith("+02:00"))

    def test_availability_rejects_past_date_without_querying_bigquery(self):
        bq = Mock()
        with (
            patch.object(agent, "_ahora_local", return_value=self.now),
            patch.object(agent, "_bq", bq),
        ):
            result = agent.consultar_disponibilidad(
                "2024-12-25", villa_nombre="ALMASUR"
            )

        self.assertFalse(result["available"])
        self.assertIn("fecha pasada", result["error"])
        self.assertEqual(result["fecha_actual"], "2026-07-19")
        bq.query.assert_not_called()

    def test_availability_uses_overlap_query_and_returns_exact_total(self):
        rows = [
            {
                "nombre": "ADORA",
                "pueblo_cercano": "CALPE",
                "total_disponibles": 2,
            },
            {
                "nombre": "ALMASUR",
                "pueblo_cercano": "CALPE",
                "total_disponibles": 2,
            },
        ]
        bq = Mock()
        bq.query.return_value = _QueryResult(rows)

        with (
            patch.object(agent, "_ahora_local", return_value=self.now),
            patch.object(agent, "_bq", bq),
        ):
            result = agent.consultar_disponibilidad(
                "2026-07-20", "2026-07-27", ubicacion="Calpe"
            )

        query = bq.query.call_args.args[0]
        self.assertIn("r.fecha_entrada < @fecha_hasta", query)
        self.assertIn("r.fecha_salida > @fecha_desde", query)
        self.assertIn("'CANCELACION'", query)
        self.assertTrue(result["available"])
        self.assertEqual(result["total_disponibles"], 2)
        self.assertEqual(len(result["matches"]), 2)
        self.assertNotIn("total_disponibles", result["matches"][0])
        self.assertNotIn("total_disponibles", result["matches"][1])

    def test_villa_detail_survives_optional_bathroom_query_failure(self):
        villa = {
            "villa_id": "villa-1",
            "nombre": "ADORA",
            "capacidad_pax": 10,
            "tiene_piscina_privada": True,
            "tiene_internet": True,
        }
        bq = Mock()
        # villa -> plantas (sin codigo_busqueda no hay consulta de ficha)
        bq.query.side_effect = [
            _QueryResult([villa]),
            RuntimeError("tabla de baños no disponible"),
        ]

        with (
            patch.object(agent, "_bq", bq),
            self.assertLogs("agente-villas", level="WARNING"),
        ):
            result = agent.obtener_detalle_propiedad("ADORA")

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["nombre"], "ADORA")
        self.assertNotIn("villa_id", result["matches"][0])
        self.assertFalse(result["detalle_banios_disponible"])

    def test_existing_villa_survives_amenities_failure_and_strips_prefix(self):
        villa = {
            "villa_id": "villa-1",
            "codigo_busqueda": "123",
            "nombre": "ADORA",
            "capacidad_pax": 10,
            "tiene_piscina_privada": True,
        }
        bq = Mock()
        # villa -> ficha -> plantas
        bq.query.side_effect = [
            _QueryResult([villa]),
            RuntimeError("tabla de ficha técnica no disponible"),
            _QueryResult([]),
        ]

        with (
            patch.object(agent, "_bq", bq),
            self.assertLogs("agente-villas", level="WARNING"),
        ):
            result = agent.obtener_detalle_propiedad("Villa ADORA")

        base_config = bq.query.call_args_list[0].kwargs["job_config"]
        base_values = {
            parameter.name: parameter.value
            for parameter in base_config.query_parameters
        }
        self.assertEqual(base_values["nombre"], "%ADORA%")
        self.assertEqual(base_values["nombre_exacto"], "ADORA")
        self.assertEqual(result["matches"][0]["nombre"], "ADORA")
        self.assertEqual(result["matches"][0]["capacidad_pax"], 10)
        self.assertFalse(result["detalle_amenidades_disponible"])
        self.assertTrue(result["detalle_banios_disponible"])

    def test_booking_status_codes_match_mapped_silver_values(self):
        bq = Mock()
        bq.query.return_value = _QueryResult([])

        with patch.object(agent, "_bq", bq):
            agent.consultar_reservas(
                estado_reserva="RE",
                estado_documento="CO",
            )

        query = bq.query.call_args.args[0]
        config = bq.query.call_args.kwargs["job_config"]
        values = {
            parameter.name: parameter.value
            for parameter in config.query_parameters
        }
        # Cada código viaja con todas sus grafías conocidas: en silver el mismo
        # estado aparece unas veces como código y otras escrito.
        self.assertIn("UPPER(r.estado_reserva) IN", query)
        self.assertIn("UPPER(r.estado_documento) IN", query)
        reserva = {v for k, v in values.items() if k.startswith("estado_reserva")}
        documento = {v for k, v in values.items() if k.startswith("estado_documento")}
        self.assertEqual({"RE", "RESERVA"}, reserva)
        self.assertEqual({"CO", "CONFIRMADA", "COMPLETADA"}, documento)

    def test_all_roles_can_check_current_time_and_availability(self):
        for role_agent in agent.AGENTS.values():
            tool_names = {tool.__name__ for tool in role_agent.tools}
            self.assertIn("obtener_fecha_hora_actual", tool_names)
            self.assertIn("consultar_disponibilidad", tool_names)

        internal_tool_names = {
            tool.__name__ for tool in agent.agent_interno.tools
        }
        self.assertIn("obtener_detalle_propiedad", internal_tool_names)


if __name__ == "__main__":
    unittest.main()
