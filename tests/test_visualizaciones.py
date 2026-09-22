"""Mapa y gráficos que acompañan a las respuestas del agente.

Salen de lo que devuelven las herramientas, nunca del texto del modelo: cada
punto y cada barra es un dato real.
"""

import asyncio
import json
import re
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import chat_app
    import visualizaciones as viz

from google.adk.sessions import InMemorySessionService


AZUL = "#3E5A9A"
NARANJA = "#eb6834"


def _spec(chart) -> str:
    return json.dumps(chart.to_dict())


# ---------------------------------------------------------------------------
# Qué visualización sale de cada herramienta
# ---------------------------------------------------------------------------

class RecogerTest(unittest.TestCase):

    def test_una_busqueda_con_resultados_da_un_mapa(self):
        r = viz.recoger([("buscar_propiedades", {"ubicacion": "Calpe"},
                          {"matches": [{"nombre": "ADORA"}, {"nombre": "ALDABA"}], "total": 2})])
        self.assertEqual(["mapa"], [v["tipo"] for v in r])
        self.assertEqual(["ADORA", "ALDABA"], [x["nombre"] for x in r[0]["villas"]])

    def test_varias_busquedas_se_juntan_en_un_solo_mapa_sin_repetir(self):
        r = viz.recoger([
            ("obtener_detalle_propiedad", {"nombre": "ADORA"}, {"matches": [{"nombre": "ADORA"}]}),
            ("obtener_detalle_propiedad", {"nombre": "DIVA"}, {"matches": [{"nombre": "DIVA"}]}),
            ("consultar_disponibilidad", {}, {"matches": [{"nombre": "ADORA"}], "total_disponibles": 1}),
        ])
        mapas = [v for v in r if v["tipo"] == "mapa"]
        self.assertEqual(1, len(mapas))
        self.assertEqual(["ADORA", "DIVA"], [x["nombre"] for x in mapas[0]["villas"]])

    def test_errores_y_resultados_vacios_no_dan_nada(self):
        r = viz.recoger([
            ("buscar_propiedades", {}, {"matches": [], "total": 0}),
            ("consultar_precios", {"villa_nombre": "ADOR"}, {"noches": [], "error": "ambigua"}),
            ("buscar_internet", {"consulta": "fiestas"}, {"respuesta": "..."}),
        ])
        self.assertEqual([], r)

    def test_precios_calendario_y_resumen(self):
        r = viz.recoger([
            ("consultar_precios", {"villa_nombre": "ADORA"},
             {"villa": "ADORA", "noches": [{"fecha": "2026-10-01", "precio_venta": 400.0, "precio_compra": 300.0, "margen": 100.0}],
              "resumen": {"margen_total": 100.0}}),
            ("calendario_villa", {"villa_nombre": "ADORA"},
             {"villa": "ADORA", "tramos": [{"desde": "2026-10-01", "hasta": "2026-10-03", "tipo_ocupacion": "Libre", "noches": 3}],
              "resumen": {"ocupacion_pct": 0.0}}),
            ("resumen_reservas", {"agrupar_por": "mes"},
             {"resumen": [{"dimension": "2026-09", "total_reservas": 5, "importe_total": 1000.0}]}),
        ])
        self.assertEqual(["precios", "calendario", "resumen_reservas"], [v["tipo"] for v in r])
        self.assertEqual("mes", r[2]["agrupar_por"])
        self.assertEqual("ADORA", r[0]["villa"])


# ---------------------------------------------------------------------------
# Coordenadas: exactas para el equipo, aproximadas para el cliente
# ---------------------------------------------------------------------------

class CoordenadasTest(unittest.TestCase):

    FILAS = [
        {"nombre": "ADORA", "latitud": "38.663368", "longitud": "0.057957", "pueblo_cercano": "Calpe", "direccion": "Calle X 1"},
        {"nombre": "DIVA", "latitud": None, "longitud": None, "pueblo_cercano": "Moraira", "direccion": "Calle Y 2"},
    ]

    def _mapa(self):
        return {"tipo": "mapa", "villas": [{"nombre": "ADORA"}, {"nombre": "DIVA"}]}

    def test_el_equipo_ve_la_posicion_exacta(self):
        r = viz.anadir_coordenadas(self._mapa(), rol="interno", consultar=lambda nombres: self.FILAS)
        adora = r["villas"][0]
        self.assertAlmostEqual(38.663368, adora["lat"], places=6)
        self.assertFalse(r["aproximado"])
        self.assertEqual("Calle X 1", adora.get("direccion"))

    def test_el_cliente_ve_una_posicion_aproximada_y_sin_direccion(self):
        r = viz.anadir_coordenadas(self._mapa(), rol="cliente", consultar=lambda nombres: self.FILAS)
        adora = r["villas"][0]
        self.assertEqual(38.66, adora["lat"])
        self.assertEqual(0.06, adora["lon"])
        self.assertTrue(r["aproximado"])
        self.assertNotIn("direccion", adora)

    def test_las_villas_sin_coordenadas_no_salen_en_el_mapa(self):
        r = viz.anadir_coordenadas(self._mapa(), rol="interno", consultar=lambda nombres: self.FILAS)
        self.assertEqual(["ADORA"], [v["nombre"] for v in r["villas"]])

    def test_si_ninguna_tiene_coordenadas_no_hay_mapa(self):
        r = viz.anadir_coordenadas(self._mapa(), rol="interno", consultar=lambda nombres: self.FILAS[1:])
        self.assertIsNone(r)


# ---------------------------------------------------------------------------
# Gráficos con la paleta validada
# ---------------------------------------------------------------------------

class CalendarioTest(unittest.TestCase):

    def test_tres_clases_de_color_mas_el_fondo_de_libre(self):
        self.assertEqual("Reserva de cliente", viz.categoria_calendario("Reserva"))
        self.assertEqual("Reserva de cliente", viz.categoria_calendario("Reserva Agencia"))
        self.assertEqual("Uso del propietario", viz.categoria_calendario("Reserva Propietario"))
        self.assertEqual("Bloqueada", viz.categoria_calendario("No Disponible"))
        self.assertEqual("Libre", viz.categoria_calendario("Libre"))

    TRAMOS = {"tipo": "calendario", "villa": "ADORA", "tramos": [
        {"desde": "2026-10-01", "hasta": "2026-10-02", "tipo_ocupacion": "Libre", "noches": 2},
        {"desde": "2026-10-03", "hasta": "2026-10-06", "tipo_ocupacion": "Reserva Agencia", "noches": 4},
        {"desde": "2026-10-07", "hasta": "2026-10-09", "tipo_ocupacion": "Reserva", "noches": 3},
        {"desde": "2026-10-10", "hasta": "2026-11-02", "tipo_ocupacion": "Reserva Propietario", "noches": 24},
    ], "resumen": {}}

    def _dias(self, h, mes="Octubre"):
        """Los días de un mes: {día: (clases, detalle)}."""
        rejilla = re.search(mes + r' 2026</div><table>(.*?)</table>', h, re.S).group(1)
        return {int(n): (clase, titulo) for clase, titulo, n in
                re.findall(r'<td class="([^"]*)"[^>]*title="([^"]*)"[^>]*>(\d+)</td>', rejilla)}

    def test_un_mes_por_rejilla_de_lunes_a_domingo(self):
        h = viz.calendario_html(self.TRAMOS)
        self.assertIn("Octubre 2026", h)
        self.assertIn("Noviembre 2026", h)
        for dia in ("L", "M", "X", "J", "V", "S", "D"):
            self.assertIn(f"<th>{dia}</th>", h)
        # El 1 de octubre de 2026 es jueves: tres huecos antes.
        primera = re.search(r"<tbody><tr>(.*?)</tr>", h).group(1)
        self.assertEqual(3, primera.count('class="abv-cal-vacio"'))

    def test_cada_dia_con_su_color_y_su_detalle(self):
        dias = self._dias(viz.calendario_html(self.TRAMOS))
        self.assertIn("abv-cal-libre", dias[1][0])
        self.assertIn("abv-cal-reserva", dias[4][0])
        self.assertIn("Reserva Agencia", dias[4][1])
        self.assertIn("03/10 – 06/10", dias[4][1])
        self.assertIn("abv-cal-propietario", dias[15][0])

    def test_reservas_seguidas_se_distinguen(self):
        dias = self._dias(viz.calendario_html(self.TRAMOS))
        self.assertIn("abv-cal-entrada", dias[7][0])
        self.assertNotIn("abv-cal-entrada", dias[8][0])

    def test_dias_fuera_del_periodo_sin_color(self):
        dias = self._dias(viz.calendario_html(self.TRAMOS), "Noviembre")
        self.assertIn("abv-cal-propietario", dias[2][0])
        self.assertIn("abv-cal-fuera", dias[30][0])

    def test_leyenda_con_la_paleta(self):
        h = viz.calendario_html(self.TRAMOS)
        for color in (AZUL, NARANJA, "#8A8780", "#E9E4D8"):
            self.assertIn(color, h, color)
        for etiqueta in ("Reserva de cliente", "Uso del propietario", "Bloqueada", "Libre"):
            self.assertIn(etiqueta, h)

    def test_estilos_acotados(self):
        css = re.search(r"<style>(.*?)</style>", viz.calendario_html(self.TRAMOS), re.S).group(1)
        for regla in re.findall(r"(?:^|})\s*([^{}@]+?)\s*{", css):
            for selector in regla.split(","):
                self.assertTrue(selector.strip().startswith(".abv-cal"), selector)

    def test_el_texto_se_escapa(self):
        v = dict(self.TRAMOS, tramos=[{"desde": "2026-10-01", "hasta": "2026-10-01",
                                       "tipo_ocupacion": "<b>x</b>", "noches": 1}])
        self.assertNotIn("<b>x</b>", viz.calendario_html(v))


class PreciosTest(unittest.TestCase):

    def _viz(self):
        return {"tipo": "precios", "villa": "ADORA", "noches": [
            {"fecha": "2026-10-01", "precio_venta": 400.0, "precio_compra": 300.0, "margen": 100.0, "margen_pct": 25.0},
            {"fecha": "2026-10-02", "precio_venta": 420.0, "precio_compra": 310.0, "margen": 110.0, "margen_pct": 26.19},
        ], "resumen": {"margen_total": 210.0}}

    def test_venta_y_compra_en_un_solo_eje_con_la_paleta(self):
        spec = _spec(viz.grafico_precios(self._viz()))
        self.assertIn(AZUL, spec)
        self.assertIn(NARANJA, spec)
        self.assertNotIn('"independent"', spec)   # nunca dos ejes Y
        self.assertIn('"strokeWidth": 2', spec)

    def test_el_margen_va_en_el_detalle(self):
        self.assertIn("margen", _spec(viz.grafico_precios(self._viz())))

    def test_el_grafico_tiene_un_punto_por_noche_y_serie(self):
        # Se pintaba vacío: las columnas de venta y compra iban a la vez como
        # identificador y como valor, y el melt las descartaba.
        d = viz.grafico_precios(self._viz()).to_dict()
        filas = next(iter(d["datasets"].values()))
        self.assertEqual(4, len(filas))
        self.assertTrue(all(isinstance(f["valor"], (int, float)) for f in filas))
        self.assertEqual({"Precio de venta", "Precio de compra"}, {f["serie"] for f in filas})


class CifrasResumenTest(unittest.TestCase):

    def test_van_en_una_fila_compacta_y_escapadas(self):
        html = viz.cifras_html([("Venta del periodo", "6.754 €"), ("Margen <b>", "30 %")])
        self.assertIn('class="abv-cifras"', html)
        self.assertEqual(2, html.count('class="abv-cifra"'))
        self.assertIn("6.754 €", html)
        self.assertIn("Margen &lt;b&gt;", html)


class ResumenReservasTest(unittest.TestCase):

    def test_por_mes_son_columnas_en_orden_temporal(self):
        v = {"tipo": "resumen_reservas", "agrupar_por": "mes", "filas": [
            {"dimension": "2026-09", "total_reservas": 5, "importe_total": 1000.0},
            {"dimension": "2026-08", "total_reservas": 9, "importe_total": 2000.0},
        ]}
        d = viz.grafico_resumen(v).to_dict()
        spec = json.dumps(d)
        self.assertIn(AZUL, spec)
        self.assertIn('"cornerRadiusEnd": 4', spec)
        self.assertEqual("dimension", d["encoding"]["x"]["field"])

    def test_por_villa_son_barras_horizontales_finas(self):
        v = {"tipo": "resumen_reservas", "agrupar_por": "villa", "filas": [
            {"dimension": "ADORA", "total_reservas": 5, "importe_total": 1000.0},
        ]}
        d = viz.grafico_resumen(v).to_dict()
        self.assertEqual("dimension", d["encoding"]["y"]["field"])
        self.assertLessEqual(d["mark"]["size"], 24)


class MapaTest(unittest.TestCase):

    def _viz(self, aproximado):
        return {"tipo": "mapa", "aproximado": aproximado, "villas": [
            {"nombre": "ADORA", "lat": 38.66, "lon": 0.06, "pueblo_cercano": "Calpe"},
            {"nombre": "ALDABA", "lat": 38.65, "lon": 0.05, "pueblo_cercano": "Calpe"},
        ]}

    def test_puntos_azules_con_anillo_blanco_y_nombre_al_pasar(self):
        deck = viz.mapa_deck(self._viz(False))
        capa = deck.layers[0]
        self.assertEqual([62, 90, 154], list(capa.get_fill_color)[:3])
        self.assertEqual([255, 255, 255], list(capa.get_line_color)[:3])
        self.assertTrue(capa.pickable)
        self.assertIn("{nombre}", json.dumps(viz.tooltip_mapa(self._viz(False))))

    def test_el_equipo_ve_la_direccion_si_la_hay(self):
        v = self._viz(False)
        v["villas"][0]["direccion"] = "Calle X 1"
        self.assertIn("{direccion}", viz.tooltip_mapa(v)["html"])

    def test_el_detalle_no_se_sale_del_mapa(self):
        estilo = viz.tooltip_mapa(self._viz(False))["style"]
        self.assertIn("maxWidth", estilo)
        self.assertEqual("normal", estilo.get("whiteSpace"))

    def test_el_cliente_no_ve_la_direccion_en_el_detalle(self):
        v = self._viz(True)
        v["villas"][0]["direccion"] = "Calle X 1"
        self.assertNotIn("direccion", json.dumps(viz.tooltip_mapa(v)))


# ---------------------------------------------------------------------------
# chat_app recoge lo que devuelven las herramientas
# ---------------------------------------------------------------------------

class _Evento:
    def __init__(self, llamadas=(), respuestas=(), texto=None):
        self._llamadas, self._respuestas, self._texto = llamadas, respuestas, texto
        self.content = Mock(parts=[Mock(text=texto)]) if texto else None

    def get_function_calls(self):
        return list(self._llamadas)

    def get_function_responses(self):
        return list(self._respuestas)

    def is_final_response(self):
        return self._texto is not None


class _RunnerConHerramienta:
    def __init__(self, **kwargs):
        pass

    async def run_async(self, *, user_id, session_id, new_message):
        yield _Evento(llamadas=[Mock(id="c1", args={"ubicacion": "Calpe"})])
        respuesta = Mock(id="c1", response={"matches": [{"nombre": "ADORA"}], "total": 1})
        respuesta.name = "buscar_propiedades"
        yield _Evento(respuestas=[respuesta])
        yield _Evento(texto="Hay 1 villa en Calpe.")


class RunAgentDevuelveHerramientasTest(unittest.TestCase):

    def test_devuelve_el_texto_y_lo_que_devolvieron_las_herramientas(self):
        with patch.object(chat_app, "_session_service", InMemorySessionService()), \
             patch.object(chat_app, "Runner", _RunnerConHerramienta), \
             patch.dict(chat_app.AGENTS, {"interno": Mock()}):
            texto, llamadas = chat_app._run_agent(
                role="interno", user_id="u@abahanavillas.com", session_id="s1", message="villas en Calpe")
        self.assertEqual("Hay 1 villa en Calpe.", texto)
        self.assertEqual([("buscar_propiedades", {"ubicacion": "Calpe"},
                           {"matches": [{"nombre": "ADORA"}], "total": 1})], llamadas)


if __name__ == "__main__":
    unittest.main()
