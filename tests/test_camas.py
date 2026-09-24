"""Las camas, una a una: tipo y tamaño del colchón.

Hasta ahora solo se sabía cuántas camas de cada clase había en cada estancia
(stg_etendo_Estancia). ovjch_camas trae cada cama con su colchón, que es lo que
pregunta el cliente ("¿la cama es de 150 o de 180?").
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class CamasEnLaFichaTest(unittest.TestCase):

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        p = patch.object(agent, "_bq", self.bq)
        p.start()
        self.addCleanup(p.stop)
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def test_la_tabla_esta_declarada(self):
        self.assertIn("stg_etendo_Cama", agent.TABLA_CAMA)

    def test_la_ficha_las_pide_por_planta(self):
        import inspect
        codigo = inspect.getsource(agent.obtener_detalle_propiedad)
        self.assertIn("TABLA_CAMA", codigo)
        self.assertIn("tamano_colchon", codigo)

    def test_cada_planta_trae_el_detalle_de_sus_camas(self):
        plantas = [{"numero_planta": 1, "planta": "PLANTA BAJA", "dormitorios": 2,
                    "camas": [{"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 2},
                              {"tipo_cama": "Cama Simple", "tamano_colchon": "90X190", "unidades": 1}]}]
        totales = agent._totales_de_plantas(plantas)
        self.assertEqual([{"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 2},
                          {"tipo_cama": "Cama Simple", "tamano_colchon": "90X190", "unidades": 1}],
                         totales["camas"])

    def test_dos_plantas_suman_las_mismas_medidas(self):
        plantas = [{"camas": [{"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 2}]},
                   {"camas": [{"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 1},
                              {"tipo_cama": "Litera", "tamano_colchon": "90X190", "unidades": 1}]}]
        camas = agent._totales_de_plantas(plantas)["camas"]
        self.assertIn({"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 3}, camas)
        self.assertEqual(2, len(camas))

    def test_sin_camas_no_se_inventa_la_clave(self):
        self.assertNotIn("camas", agent._totales_de_plantas([{"dormitorios": 1}]))


class EnLaFichaDeLaAppTest(unittest.TestCase):

    def test_la_ficha_enseña_las_medidas(self):
        import ficha
        villa = {"nombre": "ADORA", "camas": [
            {"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 2},
            {"tipo_cama": "Cama Simple", "tamano_colchon": "90X190", "unidades": 1}]}
        html = ficha.html(villa, role="interno", url=None, fotos=[], frescura=[])
        self.assertIn("2 camas dobles de 150×190", html)
        self.assertIn("1 cama simple de 90×190", html)

    def test_no_repite_el_recuento_cuando_hay_medidas(self):
        import ficha
        villa = {"nombre": "ADORA", "camas_totales": 3, "camas_dobles": 2, "camas_simples": 1,
                 "camas": [{"tipo_cama": "Cama Doble", "tamano_colchon": "150X190", "unidades": 2},
                           {"tipo_cama": "Cama Simple", "tamano_colchon": "90X190", "unidades": 1}]}
        html = ficha.html(villa, role="interno", url=None, fotos=[], frescura=[])
        self.assertIn("3 camas<", html)
        self.assertNotIn("3 camas: 2 dobles", html)

    def test_sin_medidas_sigue_el_recuento_de_siempre(self):
        import ficha
        villa = {"nombre": "ADORA", "camas_totales": 3, "camas_dobles": 2, "camas_simples": 1}
        self.assertIn("3 camas: 2 dobles, 1 individual",
                      ficha.html(villa, role="interno", url=None, fotos=[], frescura=[]))

    def test_la_tabla_por_plantas_enseña_las_medidas(self):
        import ficha
        villa = {"nombre": "ADORA", "plantas": [{"numero_planta": 0, "planta": "PLANTA BAJA",
                 "camas": [{"tipo_cama": "Cama Doble", "tamano_colchon": "150X200", "unidades": 1}]}]}
        self.assertIn("1 cama doble de 150×200",
                      ficha.html(villa, role="interno", url=None, fotos=[], frescura=[]))


if __name__ == "__main__":
    unittest.main()
