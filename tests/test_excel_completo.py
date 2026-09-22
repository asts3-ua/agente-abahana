"""El Excel lleva todas las filas, aunque la respuesta muestre un ejemplo.

La búsqueda se repite sin el tope de filas que tiene el agente (50), directa a
BigQuery y sin pasar por el modelo, y solo si la herramienta dijo que había más.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app
    import exportar


def _sql(funcion, **kwargs):
    bq = Mock()
    bq.query.return_value.result.return_value = []
    with patch.object(agent, "_bq", bq), patch.object(agent, "_nombres_villas", return_value=[]), \
         patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones"):
        funcion(**kwargs)
    return bq.query.call_args_list[0][0][0]


class ConsultaCompletaTest(unittest.TestCase):

    CASOS = {
        "consultar_reservas": {"fecha_desde": "2026-08-01"},
        "resumen_reservas": {"agrupar_por": "villa"},
        "buscar_ofertas": {"fecha_desde": "2099-10-01", "fecha_hasta": "2099-10-08"},
        "consultar_disponibilidad": {"fecha_desde": "2099-10-01", "fecha_hasta": "2099-10-08"},
    }

    def test_el_agente_sigue_viendo_como_mucho_50(self):
        for nombre, args in self.CASOS.items():
            sql = _sql(getattr(agent, nombre), **args, limite=5000)
            self.assertIn("LIMIT 50", sql, nombre)

    def test_para_el_excel_se_quita_el_tope(self):
        for nombre, args in self.CASOS.items():
            bq = Mock()
            bq.query.return_value.result.return_value = []
            with patch.object(agent, "_bq", bq), patch.object(agent, "_nombres_villas", return_value=[]), \
                 patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones"):
                agent.consulta_completa(nombre, args)
            self.assertIn(f"LIMIT {agent._TOPE_EXPORTAR}", bq.query.call_args_list[0][0][0], nombre)

    def test_despues_de_exportar_vuelve_el_tope(self):
        bq = Mock()
        bq.query.return_value.result.return_value = []
        with patch.object(agent, "_bq", bq), patch.object(agent, "_nombres_villas", return_value=[]):
            agent.consulta_completa("consultar_reservas", {})
        self.assertIn("LIMIT 50", _sql(agent.consultar_reservas, limite=5000))

    def test_no_repite_herramientas_que_no_son_listas(self):
        self.assertIsNone(agent.consulta_completa("buscar_internet", {"consulta": "x"}))


class ExcelDelTurnoTest(unittest.TestCase):

    def test_repite_solo_si_habia_mas_filas(self):
        parcial = ("consultar_reservas", {"fecha_desde": "2026-08-01", "limite": 5},
                   {"reservas": [{"localizador": f"2026_{i}"} for i in range(5)], "total": 668})
        completo = ("buscar_propiedades", {"ubicacion": "Altea"},
                    {"matches": [{"nombre": "ADORA"}], "total": 1})
        todas = {"reservas": [{"localizador": f"2026_{i}"} for i in range(668)], "total": 668}
        with patch.object(chat_app.agent, "consulta_completa", return_value=todas) as repetir:
            tablas, excel = chat_app._exportable_del_turno([parcial, completo])
        repetir.assert_called_once_with("consultar_reservas", {"fecha_desde": "2026-08-01", "limite": 5})
        self.assertEqual(668, len(tablas[0]["filas"]))
        self.assertEqual(1, len(tablas[1]["filas"]))
        self.assertIsNotNone(excel)

    def test_si_falla_la_repeticion_exporta_lo_que_habia(self):
        parcial = ("consultar_reservas", {}, {"reservas": [{"localizador": "2026_1"}], "total": 9})
        with patch.object(chat_app.agent, "consulta_completa", side_effect=RuntimeError("caído")), \
             self.assertLogs("chat-app", level="WARNING"):
            tablas, _ = chat_app._exportable_del_turno([parcial])
        self.assertEqual(1, len(tablas[0]["filas"]))


class ElBotonDiceCuantasFilasTest(unittest.TestCase):

    def test_excel_con_el_numero_de_filas(self):
        html = exportar.botones_html("hola", b"PK", "x.xlsx", filas=668)
        self.assertIn("Excel · 668", html)

    def test_el_resumen_del_excel_dice_que_esta_completo(self):
        import io, openpyxl
        tabla = {"titulo": "Reservas", "filas": [{"localizador": "a"}] * 3, "total": 3}
        hoja = openpyxl.load_workbook(io.BytesIO(exportar.a_excel([tabla])))["Reservas"]
        self.assertTrue(hoja["A2"].value.startswith("3 filas"))


if __name__ == "__main__":
    unittest.main()
