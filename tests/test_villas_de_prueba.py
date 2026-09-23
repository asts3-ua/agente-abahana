"""Las villas de prueba de Etendo (la de código "test" y las que se llaman
"prueba…") no salen en ninguna respuesta: ensucian listas, fichas y
sugerencias."""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class FueraDeLasConsultasTest(unittest.TestCase):

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        p = patch.object(agent, "_bq", self.bq)
        p.start()
        self.addCleanup(p.stop)
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def _sql(self, n=0):
        return self.bq.query.call_args_list[n][0][0]

    def test_la_condicion_deja_fuera_las_de_prueba(self):
        self.assertIn("codigo_busqueda", agent._NO_ES_PRUEBA)
        self.assertIn("test", agent._NO_ES_PRUEBA.lower())
        self.assertIn("prueba", agent._NO_ES_PRUEBA.lower())

    def test_las_villas_vigentes(self):
        self.assertIn(agent._NO_ES_PRUEBA, agent._CTE_VILLAS_VIGENTES)

    def test_la_ficha(self):
        with patch.object(agent, "_nombres_villas", return_value=[]):
            agent.obtener_detalle_propiedad("test")
        self.assertIn(agent._NO_ES_PRUEBA, self._sql())

    def test_las_sugerencias_de_nombre(self):
        agent._nombres_villas_cache = (0.0, [])
        agent._nombres_villas()
        self.assertIn(agent._NO_ES_PRUEBA, self._sql())
        agent._nombres_villas_cache = (0.0, [])

    def test_el_nombre_exacto(self):
        self.assertIn(agent._NO_ES_PRUEBA, agent._sql_nombre_villa("v.nombre"))

    def test_las_reservas(self):
        agent.consultar_reservas(limite=5)
        self.assertIn(agent._NO_ES_PRUEBA, self._sql())
        self.assertIn(agent._RESERVA_NO_ES_PRUEBA, self._sql())

    def test_el_resumen_de_reservas(self):
        agent.resumen_reservas(agrupar_por="villa")
        self.assertIn(agent._RESERVA_NO_ES_PRUEBA, self._sql())

    def test_la_disponibilidad_y_las_ofertas(self):
        agent.consultar_disponibilidad("2099-10-01", "2099-10-08")
        self.assertIn(agent._NO_ES_PRUEBA, self._sql())
        self.bq.query.reset_mock()
        agent.buscar_propiedades()
        self.assertIn(agent._NO_ES_PRUEBA, self._sql())


if __name__ == "__main__":
    unittest.main()
