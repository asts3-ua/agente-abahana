""""Calpe o Moraira" son dos pueblos, no uno llamado así.

El agente pasaba ubicacion="Calpe | Moraira", la herramienta buscaba ese texto
tal cual y respondía que no había ninguna villa.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class VariosPueblosTest(unittest.TestCase):

    def setUp(self):
        self.bq = Mock()
        self.bq.query.return_value.result.return_value = []
        for nombre, valor in (("_bq", self.bq),):
            p = patch.object(agent, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def _sql_y_params(self):
        sql = "\n".join(c[0][0] for c in self.bq.query.call_args_list)
        params = {}
        for c in self.bq.query.call_args_list:
            config = c.kwargs.get("job_config")
            for p in getattr(config, "query_parameters", []) or []:
                params[p.name] = p.value
        return sql, params

    HERRAMIENTAS = {
        "buscar_propiedades": lambda u, z: agent.buscar_propiedades(ubicacion=u, zona=z),
        "buscar_por_valoracion": lambda u, z: agent.buscar_por_valoracion(ubicacion=u),
        "consultar_disponibilidad": lambda u, z: agent.consultar_disponibilidad(
            fecha_desde="2099-10-10", fecha_hasta="2099-10-17", ubicacion=u, zona=z),
        "buscar_ofertas": lambda u, z: agent.buscar_ofertas(
            fecha_desde="2099-10-10", fecha_hasta="2099-10-17", ubicacion=u, zona=z),
        "consultar_reservas": lambda u, z: agent.consultar_reservas(ubicacion=u, zona=z),
        "resumen_reservas": lambda u, z: agent.resumen_reservas(ubicacion=u, zona=z),
    }

    def test_cualquiera_de_los_pueblos_en_cada_herramienta(self):
        for nombre, llamar in self.HERRAMIENTAS.items():
            for escrito in ("Calpe | Moraira", "Calpe, Moraira", "Calpe o Moraira", "Calpe y Moraira"):
                self.bq.query.reset_mock()
                llamar(escrito, None)
                sql, params = self._sql_y_params()
                valores = {v for k, v in params.items() if k.startswith("ubicacion")}
                self.assertEqual({"%Calpe%", "%Moraira%"}, valores, f"{nombre}: {escrito}")
                self.assertIn(" OR ", sql, f"{nombre}: {escrito}")

    def test_varias_zonas(self):
        self.bq.query.reset_mock()
        agent.buscar_ofertas(fecha_desde="2099-10-10", fecha_hasta="2099-10-17",
                             zona="Moraira | Benissa Costa")
        _, params = self._sql_y_params()
        self.assertEqual({"%Moraira%", "%Benissa Costa%"},
                         {v for k, v in params.items() if k.startswith("zona")})

    def test_un_solo_pueblo_no_cambia(self):
        agent.buscar_propiedades(ubicacion="Calpe")
        sql, params = self._sql_y_params()
        self.assertIn("LOWER(v.pueblo_cercano) LIKE LOWER(@ubicacion)", sql)
        self.assertEqual("%Calpe%", params["ubicacion"])



class PrecioDeUnaEstanciaTest(unittest.TestCase):
    """consultar_precios cuenta hasta la última noche incluida; el resto de
    herramientas usa el día de salida. Pasarle la salida cobraba una noche de
    más (NICEVIEW del 10 al 17: 756 € en vez de 670 €)."""

    def test_la_herramienta_avisa_de_la_ultima_noche(self):
        doc = " ".join(agent.consultar_precios.__doc__.split())
        self.assertIn("pasa el día anterior a la salida", doc)

    def test_las_instrucciones_mandan_a_precio_total(self):
        for rol in ("interno", "admin"):
            texto = " ".join(agent.AGENTS[rol].instruction.split())
            self.assertIn("usa el `precio_total` de `buscar_ofertas`", texto, rol)


if __name__ == "__main__":
    unittest.main()
