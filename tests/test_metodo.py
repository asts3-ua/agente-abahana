"""Cómo ha calculado el agente lo que dice, para poder comprobarlo.

Un porcentaje de ocupación no se puede verificar leyéndolo: hay que saber qué
noches entraron en el numerador y cuáles en el denominador. "Ocupación del
66 %" y "del 77 %" son la misma villa contada con dos criterios distintos.

Lo que se enseña sale del nombre de la herramienta, de sus argumentos y de los
números que devolvió: nunca del texto del modelo. Si el modelo cuenta una cosa
y la herramienta calculó otra, aquí se ve la diferencia.
"""

import inspect
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app
    import metodo


CALENDARIO = {
    "periodo": {"desde": "2026-06-01", "hasta": "2026-06-30"},
    "resumen": {
        "noches_vendidas": 18,
        "noches_libres": 6,
        "noches_bloqueadas": 3,
        "noches_propietario": 3,
        "noches_comercializables": 24,
        "ocupacion_pct": 75.0,
    },
}


class OcupacionTest(unittest.TestCase):
    """El caso que motivó todo: que se vea qué noches cuentan."""

    def _explicacion(self, nombre="calendario_villa", args=None, respuesta=None):
        bloques = metodo.explicar([(nombre, args or {}, respuesta or CALENDARIO)])
        self.assertEqual(1, len(bloques))
        return bloques[0]

    def test_la_formula_lleva_los_numeros_de_verdad(self):
        formula = self._explicacion()["formula"]
        # Numerador, denominador y resultado, para rehacer la cuenta a mano.
        self.assertIn("18", formula)
        self.assertIn("24", formula)
        self.assertIn("75", formula)

    def test_dice_que_las_del_propietario_no_cuentan(self):
        texto = " ".join(self._explicacion()["criterios"]).lower()
        self.assertIn("propietario", texto)
        self.assertIn("bloquead", texto)

    def test_las_excluidas_salen_con_su_recuento(self):
        texto = " ".join(self._explicacion()["criterios"])
        # 3 del propietario y 3 bloqueadas: si el usuario no está de acuerdo
        # con dejarlas fuera, sabe cuántas noches mueve la decisión.
        self.assertIn("3", texto)

    def test_dice_de_que_tabla_sale(self):
        self.assertIn("stg_etendo_Ocupacion",
                      " ".join(self._explicacion()["fuentes"]))

    def test_tambien_para_el_resumen_de_varias_villas(self):
        bloque = self._explicacion(
            "resumen_ocupacion",
            {"fecha_desde": "2026-06-01", "fecha_hasta": "2026-06-30"},
            {"resumen": [{"dimension": "ADORA", "noches_vendidas": 18,
                          "noches_libres": 6, "noches_comercializables": 24,
                          "ocupacion_pct": 75.0}],
             "totales": CALENDARIO["resumen"]})
        self.assertIn("propietario", " ".join(bloque["criterios"]).lower())
        self.assertIn("18", bloque["formula"])

    def test_sin_noches_comercializables_no_inventa_una_division(self):
        bloque = self._explicacion(respuesta={"resumen": {
            "noches_vendidas": 0, "noches_libres": 0, "noches_bloqueadas": 5,
            "noches_propietario": 0, "noches_comercializables": 0,
            "ocupacion_pct": None}})
        self.assertNotIn("/ 0", bloque.get("formula") or "")


class ReservasTest(unittest.TestCase):

    def test_el_resumen_explica_que_deja_fuera(self):
        bloque = metodo.explicar([("resumen_reservas",
                                   {"solo_en_firme": True, "incluir_propietario": False},
                                   {"resumen": [{"dimension": "ADORA"}], "count": 1})])[0]
        texto = " ".join(bloque["criterios"]).lower()
        self.assertIn("firme", texto)
        self.assertIn("propietario", texto)

    def test_si_piden_incluirlas_tambien_se_dice(self):
        bloque = metodo.explicar([("resumen_reservas",
                                   {"solo_en_firme": False, "incluir_propietario": True},
                                   {"resumen": [], "count": 0})])[0]
        texto = " ".join(bloque["criterios"]).lower()
        self.assertIn("incluyendo", texto)


class SqlAMedidaTest(unittest.TestCase):
    """Cuando el modelo se escribe la consulta, la consulta ES el método."""

    def test_se_enseña_el_sql_tal_cual(self):
        sql = "SELECT COUNT(*) FROM `abahanaweb.silver_clean.stg_etendo_Villa`"
        bloque = metodo.explicar([("ejecutar_sql", {"query": sql},
                                   {"rows": [{"f0_": 3}], "count": 1})])[0]
        self.assertEqual(sql, bloque.get("sql"))

    def test_una_consulta_que_fallo_no_se_presenta_como_calculo(self):
        self.assertEqual([], metodo.explicar(
            [("ejecutar_sql", {"query": "SELECT 1"}, {"error": "nope", "rows": []})]))


class SoloLoQueTieneCuentaTest(unittest.TestCase):

    def test_las_herramientas_sin_calculo_no_salen(self):
        for nombre in ("obtener_fecha_hora_actual", "generar_imagen", "buscar_internet"):
            self.assertEqual([], metodo.explicar([(nombre, {}, {"ok": True})]), nombre)

    def test_nada_no_rompe(self):
        self.assertEqual([], metodo.explicar([]))

    def test_una_respuesta_rara_no_rompe(self):
        for respuesta in (None, "texto", [], {"resumen": None}):
            metodo.explicar([("calendario_villa", {}, respuesta)])


class MismoCriterioQueElCodigoTest(unittest.TestCase):
    """Lo que se explica tiene que ser lo que de verdad se calcula."""

    def test_la_lista_de_excluidas_sale_del_agente(self):
        self.assertEqual(agent._ESTADOS_FUERA_DE_VENTA,
                         metodo.FUERA_DE_VENTA)

    def test_el_propietario_esta_excluido_en_el_agente(self):
        self.assertIn("Reserva Propietario", agent._ESTADOS_FUERA_DE_VENTA)


class DisponibilidadTest(unittest.TestCase):
    """La única con cuenta que ve también el cliente."""

    def _bloque(self):
        return metodo.explicar([("consultar_disponibilidad",
                                 {"fecha_desde": "2026-07-01", "fecha_hasta": "2026-07-08"},
                                 {"total_disponibles": 12, "matches": [],
                                  "periodo": {"desde": "2026-07-01", "hasta": "2026-07-08"}})])[0]

    def test_explica_que_una_cancelada_no_ocupa(self):
        self.assertIn("cancelada", " ".join(self._bloque()["criterios"]).lower())

    def test_explica_que_el_calendario_tambien_bloquea(self):
        self.assertIn("calendario", " ".join(self._bloque()["criterios"]).lower())

    def test_no_se_le_cuelan_datos_internos_al_cliente(self):
        texto = " ".join(self._bloque()["criterios"] + self._bloque()["fuentes"]).lower()
        for interno in ("margen", "precio de compra", "importe", "titular"):
            self.assertNotIn(interno, texto)

    def test_la_tiene_el_agente_de_cliente(self):
        nombres = {t.__name__ for t in agent.AGENTS["cliente"].tools}
        self.assertIn("consultar_disponibilidad", nombres)


class OfertasTest(unittest.TestCase):
    """La ruta que el modelo elige de verdad para "libres en agosto"."""

    def _bloque(self, **datos):
        base = {"total": 7, "total_libres": 12, "matches": [],
                "periodo": {"desde": "2027-08-10", "hasta": "2027-08-17"}}
        return metodo.explicar([("buscar_ofertas", {}, {**base, **datos})])[0]

    def test_hereda_los_criterios_de_disponibilidad(self):
        self.assertIn("calendario", " ".join(self._bloque()["criterios"]).lower())

    def test_distingue_las_libres_de_las_que_cumplen_lo_pedido(self):
        texto = " ".join(self._bloque()["criterios"])
        self.assertIn("12", texto)
        self.assertIn("7", texto)

    def test_avisa_de_que_el_precio_no_lleva_los_obligatorios(self):
        self.assertIn("precio_final_villa", " ".join(self._bloque()["criterios"]))

    def test_si_no_sobra_ninguna_no_marea_con_la_resta(self):
        criterios = " ".join(self._bloque(total_libres=7)["criterios"])
        self.assertNotIn("cumplen además", criterios)


class TodosLosRolesTest(unittest.TestCase):

    def test_el_desplegable_no_depende_del_rol(self):
        """Se pinta en el histórico, que es común a los tres agentes."""
        codigo = inspect.getsource(chat_app._render_chat_history)
        self.assertIn("_render_metodo(msg)", codigo)
        self.assertNotIn("role", inspect.getsource(chat_app._render_metodo))


class EnLaAppTest(unittest.TestCase):

    def test_va_en_un_desplegable_despues_de_la_respuesta(self):
        codigo = inspect.getsource(chat_app._render_metodo)
        self.assertIn("st.expander", codigo)
        # Plegado: no estorba a quien solo quiere la respuesta.
        self.assertIn("expanded=False", codigo)

    def test_se_pinta_en_el_historico(self):
        self.assertIn("_render_metodo(msg)",
                      inspect.getsource(chat_app._render_chat_history))

    def test_el_turno_lo_calcula_y_lo_lleva(self):
        codigo = inspect.getsource(chat_app._process_user_prompt)
        self.assertIn("metodo_turno = _metodo_del_turno(herramientas)", codigo)
        # Al mensaje en pantalla y al almacén: el mismo, calculado una vez.
        self.assertIn('"metodo": metodo_turno', codigo)
        self.assertIn("metodo=metodo_turno", codigo)

    def test_nunca_tumba_la_respuesta(self):
        with patch.object(chat_app.metodo, "explicar", side_effect=RuntimeError("boom")):
            self.assertEqual([], chat_app._metodo_del_turno([("calendario_villa", {}, {})]))


if __name__ == "__main__":
    unittest.main()
