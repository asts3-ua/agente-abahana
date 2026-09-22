"""Botón "Ficha" en las respuestas: toda la información de la villa de un
vistazo, con el diseño de la web, sin tener que hacer otra pregunta."""

import inspect
import unittest
from unittest.mock import Mock, patch

with patch("google.cloud.bigquery.Client", Mock()):
    import agent
    import chat_app
    import ficha

LIGHTHOUSE = {
    "villa_id": "X1", "nombre": "LIGHTHOUSE", "numero_habitaciones": 5,
    "tipovilla_descripcion": "Villa con piscina privada", "anio_construccion": "1992",
    "m2_parcela": 926, "m2_habitables": 258, "capacidad_pax": 10, "numero_banos": 4,
    "numero_aseos": 1, "numero_plantas": 3, "tiene_piscina_privada": True,
    "piscina_largo_m": 4, "piscina_ancho_m": 8, "piscina_climatizada": False,
    "admite_animales": False, "pueblo_cercano": "Benitachell", "zona": "Moraira",
    "region": "Costa Blanca", "latitud": "38.70", "longitud": "0.16",
    "propietario_nombre": "41030384 - Schmitz, Ulrich", "es_recomendada": True,
    "tiene_internet": True, "tiene_aire_acondicionado": True, "tiene_vista_mar": True,
    "zona_tranquila": True, "distancia_mar_m": 50, "distancia_playa_arena_m": 2400,
    "tiene_barbacoa": True, "tiene_barbacoa_obra": True, "barbacoa_plancha": "Eléctrica",
    "tipo_parcela": "Cerrada", "parcela_cerrada": True, "terreno_parcela": None,
    "score_rating_vistas": 5, "clasificacion": 5, "tiene_garaje": True, "plazas_garaje": 1,
    "licencia_vut": "CV-VUT0463654-A", "importe_fianza": 1000, "comision_pct": 25,
    "alarma_desactivacion": "Código 1234 y tecla OFF",
    "dato_nuevo_de_etendo": "algo",
    "plantas": [{"numero_planta": 0, "planta": "PLANTA BAJA", "dormitorios": 1,
                 "camas_totales": 1, "camas_dobles": 1, "banios": 1, "banios_con_ducha": 1}],
}


class VillasDelTurnoTest(unittest.TestCase):

    def test_de_listas_y_de_reservas_sin_repetir(self):
        herramientas = [
            ("buscar_propiedades", {}, {"matches": [{"nombre": "ADORA"}, {"nombre": "LIGHTHOUSE"}]}),
            ("consultar_reservas", {}, {"reservas": [{"villa_nombre": "ADORA"}, {"villa_nombre": "DIVA"}]}),
            ("buscar_internet", {}, {"respuesta": "nada"}),
        ]
        self.assertEqual(["ADORA", "LIGHTHOUSE", "DIVA"], ficha.villas_del_turno(herramientas))

    def test_la_ficha_consultada(self):
        self.assertEqual(["LIGHTHOUSE"], ficha.villas_del_turno(
            [("obtener_detalle_propiedad", {"nombre": "light"}, {"matches": [LIGHTHOUSE]})]))

    def test_como_mucho_unas_pocas(self):
        muchas = [("listar_propiedades", {}, {"matches": [{"nombre": f"V{i}"} for i in range(40)]})]
        self.assertEqual(ficha.MAXIMO_BOTONES, len(ficha.villas_del_turno(muchas)))

    def test_respuestas_raras_no_rompen(self):
        self.assertEqual([], ficha.villas_del_turno([("x", {}, None), ("y", {}, {"matches": "no"})]))


class HtmlTest(unittest.TestCase):

    def _html(self, **kw):
        base = dict(role="interno", url="https://www.abahanavillas.com/es/x/lighthouse",
                    fotos=[], frescura=["Villas: último cambio el mié 9 sep"])
        base.update(kw)
        return ficha.html(dict(LIGHTHOUSE), **base)

    def test_cabecera_como_la_web(self):
        h = self._html()
        self.assertIn("LIGHTHOUSE", h)
        self.assertIn("10 huéspedes", h)
        self.assertIn("50 m al mar", h)
        self.assertIn("★★★★★", h)
        self.assertIn('href="https://www.abahanavillas.com/es/x/lighthouse"', h)

    def test_datos_con_formato(self):
        h = self._html()
        self.assertIn("2,4 km", h)
        self.assertIn("Barbacoa de obra", h)
        self.assertIn("Parcela cerrada", h)
        self.assertIn("No se admiten mascotas", h)
        self.assertIn("PLANTA BAJA", h.upper())

    def test_lo_que_no_consta_se_dice(self):
        self.assertIn("Terreno de la parcela: no consta", self._html())

    def test_ningun_dato_se_pierde(self):
        self.assertIn("algo", self._html())

    def test_lo_ya_mostrado_no_se_repite_en_otros_datos(self):
        h = ficha.html(dict(LIGHTHOUSE, dato_nuevo_de_etendo=None, tiene_lavadora=True,
                            num_lavadoras=1, tiene_tv=True, tiene_smart_tv=True,
                            tiene_chimenea=False, num_chimeneas=None, tiene_parking_calle=True),
                       role="interno", url=None, fotos=[], frescura=[])
        self.assertNotIn("Otros datos", h)

    def test_sin_repetir_el_pueblo_si_es_la_zona(self):
        h = ficha.html(dict(LIGHTHOUSE, pueblo_cercano="Moraira"), role="interno",
                       url=None, fotos=[], frescura=[])
        self.assertNotIn("Moraira / Moraira", h)
        self.assertNotIn("Moraira · Moraira", h)

    def test_el_personal_ve_lo_interno_y_el_acceso_plegado(self):
        h = self._html()
        self.assertIn("CV-VUT0463654-A", h)
        self.assertIn("1.000 €", h)
        self.assertIn("<details", h)
        self.assertIn("Código 1234", h)

    def test_el_cliente_no_ve_nada_interno(self):
        h = self._html(role="cliente")
        for oculto in ("CV-VUT0463654-A", "Schmitz", "Código 1234", "1.000 €", "25 %"):
            self.assertNotIn(oculto, h)

    def test_escapa_el_texto(self):
        h = ficha.html(dict(LIGHTHOUSE, nombre="<script>alert(1)</script>"),
                       role="interno", url=None, fotos=[], frescura=[])
        self.assertNotIn("<script>alert(1)", h)

    def test_fotos_y_sin_fotos(self):
        foto = "https://www.abahanavillas.com/documents/1/2/1341_01.webp/abc"
        self.assertIn(foto, self._html(fotos=[foto]))
        self.assertNotIn('class="abv-galeria', self._html(fotos=[]))

    def test_estilos_acotados_a_la_ficha(self):
        # st.html mete el CSS en la página: no puede tocar el resto de la app.
        import re
        css = re.search(r"<style>(.*?)</style>", self._html(), re.S).group(1)
        for regla in re.findall(r"(?:^|})\s*([^{}@]+?)\s*{", css):
            for selector in regla.split(","):
                self.assertTrue(selector.strip().startswith(".abv-ficha"), selector)


class FotosTest(unittest.TestCase):

    PAGINA = """<img src="/documents/20182/3292867/1341_02.webp/d6e?version=1.0&amp;t=2">
        <img src="/documents/20182/3292867/1341_01.webp/686?version=1.0&amp;t=1">
        <img src="https://otro.com/documents/1/2/9_01.webp/zzz">
        <img src="/documents/20182/3292867/1341_01.webp/686?version=1.0&amp;t=1">"""

    def test_las_fotos_de_la_villa_en_orden(self):
        respuesta = Mock(status_code=200, text=self.PAGINA)
        with patch("ficha.requests.get", return_value=respuesta):
            fotos = ficha.fotos_de_la_web("https://www.abahanavillas.com/es/x/lighthouse")
        self.assertEqual(2, len(fotos))
        self.assertTrue(fotos[0].startswith("https://www.abahanavillas.com/documents/20182/3292867/1341_01.webp"))
        self.assertNotIn("&amp;", fotos[0])

    def test_fotos_con_guion(self):
        pagina = '<img src="/documents/20182/3293658/1395-01.webp/4a3?version=1.1&amp;t=1">'
        with patch("ficha.requests.get", return_value=Mock(status_code=200, text=pagina)):
            fotos = ficha.fotos_de_la_web("https://www.abahanavillas.com/es/x/mar-coral")
        self.assertEqual(1, len(fotos))
        self.assertEqual("1395", ficha.referencia(fotos))

    def test_solo_de_la_web_de_abahana(self):
        with patch("ficha.requests.get") as get:
            self.assertEqual([], ficha.fotos_de_la_web("https://abahanavillas.com.atacante.io/x"))
            get.assert_not_called()

    def test_si_la_web_falla_no_hay_fotos(self):
        with patch("ficha.requests.get", side_effect=TimeoutError()):
            self.assertEqual([], ficha.fotos_de_la_web("https://www.abahanavillas.com/es/x"))

    def test_la_referencia_sale_de_las_fotos(self):
        self.assertEqual("1341", ficha.referencia(
            ["https://www.abahanavillas.com/documents/20182/3292867/1341_01.webp/686"]))


class FichaCompletaTest(unittest.TestCase):

    def test_todas_las_secciones_para_el_personal(self):
        with patch.object(agent, "obtener_detalle_propiedad",
                          return_value={"matches": [dict(LIGHTHOUSE)]}) as detalle, \
             patch.object(agent, "_url_web_villa", return_value="https://www.abahanavillas.com/x"):
            v = agent.ficha_completa("LIGHTHOUSE", con_internos=True)
        self.assertEqual(set(agent._SECCIONES_FICHA), set(detalle.call_args.kwargs["secciones"]))
        self.assertEqual("https://www.abahanavillas.com/x", v["url_web"])

    def test_sin_lo_interno_para_el_cliente(self):
        with patch.object(agent, "obtener_detalle_propiedad",
                          return_value={"matches": [dict(LIGHTHOUSE)]}) as detalle, \
             patch.object(agent, "_url_web_villa", return_value=None):
            v = agent.ficha_completa("LIGHTHOUSE", con_internos=False)
        for interna in ("licencia", "comercial", "acceso_seguridad"):
            self.assertNotIn(interna, detalle.call_args.kwargs["secciones"])
        self.assertNotIn("propietario_nombre", v)

    def test_elige_la_de_ese_nombre_exacto(self):
        with patch.object(agent, "obtener_detalle_propiedad", return_value={"matches": [
                {"nombre": "LIGHTHOUSE II"}, {"nombre": "LIGHTHOUSE"}]}), \
             patch.object(agent, "_url_web_villa", return_value=None):
            self.assertEqual("LIGHTHOUSE", agent.ficha_completa("LIGHTHOUSE", con_internos=True)["nombre"])

    def test_si_no_existe(self):
        with patch.object(agent, "obtener_detalle_propiedad", return_value={"matches": []}):
            self.assertIsNone(agent.ficha_completa("NADA", con_internos=True))


class AppTest(unittest.TestCase):

    def test_la_respuesta_guarda_sus_villas(self):
        self.assertIn('"villas": ficha.villas_del_turno(herramientas)',
                      inspect.getsource(chat_app._process_user_prompt))

    def test_la_ficha_se_abre_desde_el_cuerpo_de_la_pagina(self):
        self.assertIn("_abrir_ficha(role)", inspect.getsource(chat_app.main))

    def test_precios_y_calendario_preguntan_al_asistente(self):
        codigo = inspect.getsource(chat_app._dialogo_ficha)
        self.assertIn("pending_prompt", codigo)

    def test_la_imagen_incluye_el_modulo(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile")) as f:
            self.assertIn("COPY ficha.py .", f.read())


if __name__ == "__main__":
    unittest.main()
