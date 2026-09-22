"""Si el nombre coincide entero con una villa, es esa y solo esa: "PUNTA VISTA"
no debe traer también "PUNTA VISTA 6". Si no coincide entero con ninguna, se
sigue buscando por parte del nombre ("punta", "Lighthous")."""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class NombreExactoTest(unittest.TestCase):

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        p = patch.object(agent, "_bq", self.bq)
        p.start()
        self.addCleanup(p.stop)
        for nombre, valor in (("_nombres_villas", []), ("_columna_habitaciones", "numero_habitaciones")):
            p = patch.object(agent, nombre, return_value=valor)
            p.start()
            self.addCleanup(p.stop)

    def _consulta(self, n=0):
        llamada = self.bq.query.call_args_list[n]
        sql = llamada[0][0]
        config = llamada.kwargs.get("job_config") or (llamada[0][1] if len(llamada[0]) > 1 else None)
        params = {p.name: p.value for p in config.query_parameters}
        return sql, params

    def _comprueba(self, sql, params, exacto="PUNTA VISTA"):
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("= LOWER(@villa_nombre_exacto)", sql)
        self.assertEqual(exacto, params["villa_nombre_exacto"])
        self.assertEqual(f"%{exacto}%", params["villa_nombre"])

    def test_ficha(self):
        agent.obtener_detalle_propiedad("PUNTA VISTA")
        self._comprueba(*self._consulta())

    def test_disponibilidad(self):
        agent.consultar_disponibilidad("2099-10-01", "2099-10-08", villa_nombre="PUNTA VISTA")
        self._comprueba(*self._consulta())

    def test_precios_y_calendario(self):
        agent.consultar_precios("PUNTA VISTA", "2099-10-01", "2099-10-08")
        self._comprueba(*self._consulta())
        self.bq.query.reset_mock()
        agent.calendario_villa("PUNTA VISTA", "2099-10-01", "2099-10-08")
        self._comprueba(*self._consulta())

    def test_alternativas(self):
        agent.alternativas_villa("PUNTA VISTA", "2099-10-01", "2099-10-08")
        self._comprueba(*self._consulta())

    def test_reservas_y_resumen(self):
        agent.consultar_reservas(villa_nombre="PUNTA VISTA")
        self._comprueba(*self._consulta())
        self.bq.query.reset_mock()
        agent.resumen_reservas(villa_nombre="PUNTA VISTA")
        self._comprueba(*self._consulta())

    def test_sin_espacios_de_mas(self):
        agent.consultar_reservas(villa_nombre="  PUNTA VISTA ")
        self._comprueba(*self._consulta())


if __name__ == "__main__":
    unittest.main()
