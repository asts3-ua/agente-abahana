"""La ficha técnica vive ahora en stg_etendo_Villa.

La vista manual stg_etendo_opxdes_ficha_tecnica no deduplicaba, leía el aire
acondicionado de un campo que solo vale 1 en una villa y se va a borrar.
"""

import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent


class _Fila(dict):
    """Imita una Row de BigQuery: se comporta como dict con .items()."""


def _bq_vacio():
    bq = Mock()
    bq.query.return_value.result.return_value = []
    return bq


def _todas_las_consultas(bq) -> str:
    return "\n".join(llamada[0][0] for llamada in bq.query.call_args_list)


class SinVistaDeFichaTest(unittest.TestCase):

    def setUp(self):
        col = patch.object(agent, "_columna_habitaciones", return_value="numero_habitaciones")
        col.start()
        self.addCleanup(col.stop)

    def test_ninguna_busqueda_consulta_la_vista_antigua(self):
        for llamada in (
            lambda: agent.buscar_propiedades(internet=True, vista_mar=True),
            lambda: agent.buscar_por_valoracion(rating_min=4),
        ):
            bq = _bq_vacio()
            with patch.object(agent, "_bq", bq):
                llamada()
            self.assertNotIn("stg_etendo_opxdes_ficha_tecnica", _todas_las_consultas(bq))

    def test_la_ficha_detallada_tampoco(self):
        villa = _Fila(villa_id="v1", nombre="ADORA")
        bq = Mock()
        bq.query.side_effect = [
            Mock(**{"result.return_value": [villa]}),
            Mock(**{"result.return_value": []}),
        ]
        with patch.object(agent, "_bq", bq):
            agent.obtener_detalle_propiedad("ADORA", secciones=["valoraciones"])
        self.assertNotIn("stg_etendo_opxdes_ficha_tecnica", _todas_las_consultas(bq))

    def test_el_aire_acondicionado_sale_de_los_equipos_reales(self):
        bq = _bq_vacio()
        with patch.object(agent, "_bq", bq):
            agent.buscar_propiedades(aire_acondicionado=True)
        sql = _todas_las_consultas(bq)
        self.assertIn("v.tiene_aire_acondicionado = TRUE", sql)
        self.assertNotIn("tiene_aire_salon", sql)

    def test_la_valoracion_media_sale_de_la_villa(self):
        bq = _bq_vacio()
        with patch.object(agent, "_bq", bq):
            agent.buscar_por_valoracion()
        self.assertIn("v.score_rating_banos", _todas_las_consultas(bq))


class FichaEnUnaSolaConsultaTest(unittest.TestCase):

    def _detalle(self, **kwargs):
        villa = _Fila(villa_id="v1", nombre="ADORA", num_mosquiteras=3)
        bq = Mock()
        bq.query.side_effect = [
            Mock(**{"result.return_value": [villa]}),   # villa con su ficha
            Mock(**{"result.return_value": []}),        # plantas
        ]
        with patch.object(agent, "_bq", bq):
            resultado = agent.obtener_detalle_propiedad("ADORA", **kwargs)
        return resultado, bq

    def test_la_ficha_llega_en_la_consulta_de_la_villa(self):
        _, bq = self._detalle(secciones=["equipamiento"])
        self.assertEqual(2, bq.query.call_count)
        self.assertIn("num_mosquiteras", bq.query.call_args_list[0][0][0])

    def test_las_mosquiteras_se_devuelven(self):
        resultado, _ = self._detalle(secciones=["equipamiento"])
        self.assertEqual(3, resultado["matches"][0]["num_mosquiteras"])

    def test_por_defecto_no_trae_las_secciones(self):
        _, bq = self._detalle()
        sql = bq.query.call_args_list[0][0][0]
        self.assertIn("tiene_aire_acondicionado", sql)
        self.assertNotIn("num_mosquiteras", sql)
        self.assertNotIn("alarma_activacion", sql)

    def test_acceso_y_seguridad_incluye_lo_nuevo(self):
        for columna in ("puerta_garaje", "wifi_num_cliente", "alarma_desactivacion"):
            self.assertIn(columna, agent._SECCIONES_FICHA["acceso_seguridad"], columna)


class SeccionesSinColumnasFantasmaTest(unittest.TestCase):
    """Columnas que se quitaron de la villa por estar vacías o mal tipadas."""

    def test_no_pide_columnas_que_ya_no_existen(self):
        todas = {c for cols in agent._SECCIONES_FICHA.values() for c in cols}
        for columna in ("zona_animada", "zona_centrica", "tiene_piscina_infantil",
                        "accesible_silla_ruedas", "distancia_farmacia_m",
                        "tipo_barbacoa_codigo", "barbacoa_portatil_codigo",
                        "tiene_aire_salon"):
            self.assertNotIn(columna, todas, columna)

    def test_ofrece_lo_que_antes_no_estaba(self):
        todas = {c for cols in agent._SECCIONES_FICHA.values() for c in cols}
        for columna in ("num_mosquiteras", "tiene_tv_satelite", "score_rating_equipamiento",
                        "tiene_petanca", "distancia_banco_m",
                        # Listas de Etendo traducidas en Dataform.
                        "tipo_cafetera", "tv_satelite", "tipo_cesped", "tipo_piscina"):
            self.assertIn(columna, todas, columna)


if __name__ == "__main__":
    unittest.main()
