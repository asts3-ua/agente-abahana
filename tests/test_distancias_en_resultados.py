"""Toda lista de villas trae la distancia al mar y a la playa.

Tras unas alternativas (que no la traían), el agente dijo que WATERFRONT era la
más cercana "a 400 m": estaba a 2,3 km, y la más cercana era LOVELY (600 m).
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class _Consultas(unittest.TestCase):

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        for objetivo, valor in ((agent, "_bq"), ):
            p = patch.object(objetivo, valor, self.bq)
            p.start()
            self.addCleanup(p.stop)
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def _todas(self) -> str:
        return "\n".join(c[0][0] for c in self.bq.query.call_args_list)

    def _con_distancias(self):
        sql = self._todas()
        self.assertIn("distancia_mar_m", sql)
        self.assertIn("distancia_playa_arena_m", sql)


class DistanciasEnCadaListaTest(_Consultas):

    def test_busqueda(self):
        agent.buscar_propiedades(ubicacion="Moraira")
        self._con_distancias()

    def test_disponibilidad(self):
        agent.consultar_disponibilidad(fecha_desde="2099-10-05", fecha_hasta="2099-10-12")
        self._con_distancias()

    def test_ofertas_y_alternativas(self):
        # alternativas_villa usa la misma consulta que buscar_ofertas.
        agent.buscar_ofertas(fecha_desde="2099-10-05", fecha_hasta="2099-10-12")
        self._con_distancias()

    def test_un_cero_es_sin_dato(self):
        agent.buscar_ofertas(fecha_desde="2099-10-05", fecha_hasta="2099-10-12")
        self.assertIn("NULLIF(v.distancia_playa_arena_m, 0)", self._todas())


class NoInventarDatosTest(unittest.TestCase):

    def test_si_falta_un_dato_se_consulta_la_ficha(self):
        for rol in ("interno", "admin"):
            instruccion = " ".join(agent.AGENTS[rol].instruction.split())
            self.assertIn("Nunca lo estimes", instruccion, rol)
            self.assertIn('secciones=["distancias"]', instruccion, rol)

    def test_mar_y_playa_no_son_lo_mismo(self):
        for rol in ("cliente", "interno", "admin"):
            self.assertIn("distancia_playa_arena_m", agent.AGENTS[rol].instruction, rol)


if __name__ == "__main__":
    unittest.main()
