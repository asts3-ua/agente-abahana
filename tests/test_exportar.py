"""Copiar o exportar lo que responde el agente: texto para un correo o un chat,
listas para una hoja de seguimiento. Sale de los datos de las herramientas, no
del texto del modelo."""

import datetime
import io
import os
import unittest

import pandas as pd

import exportar

RESERVAS = ("consultar_reservas", {"fecha_desde": "2026-09-01"}, {
    "reservas": [
        {"localizador": "2026_3274", "villa_nombre": "MARACALA", "cliente_nombre": "Farley, Olivia",
         "fecha_entrada": "2026-07-25", "fecha_salida": "2026-08-15", "importe_total": 13074.0},
        {"localizador": "2026_2550", "villa_nombre": "EL MANZANO", "cliente_nombre": "Van de Hel, Martijn",
         "fecha_entrada": "2026-07-06", "fecha_salida": "2026-08-14", "importe_total": 41339.25},
    ],
    "count": 2, "total": 463,
})
OFERTAS = ("buscar_ofertas", {}, {"matches": [
    {"villa_id": "X1", "nombre": "NICEVIEW", "pueblo_cercano": "Calpe", "capacidad_pax": 8,
     "precio_total": 670.0, "caracteristicas": ["barbacoa", "vistas"]},
], "total": 1})


class TablasTest(unittest.TestCase):

    def test_una_tabla_por_lista_devuelta(self):
        tablas = exportar.tablas([RESERVAS, OFERTAS, ("buscar_internet", {}, {"respuesta": "x"})])
        self.assertEqual(["Reservas", "Ofertas"], [t["titulo"] for t in tablas])

    def test_avisa_si_la_lista_es_parcial(self):
        reservas = exportar.tablas([RESERVAS])[0]
        self.assertEqual(463, reservas["total"])
        self.assertEqual(2, len(reservas["filas"]))

    def test_sin_identificadores_internos_y_listas_en_texto(self):
        fila = exportar.tablas([OFERTAS])[0]["filas"][0]
        self.assertNotIn("villa_id", fila)
        self.assertEqual("barbacoa, vistas", fila["caracteristicas"])

    def test_respuestas_con_error_o_vacias_no_dan_tabla(self):
        self.assertEqual([], exportar.tablas([
            ("consultar_reservas", {}, {"reservas": [], "total": 0}),
            ("buscar_ofertas", {}, {"matches": [], "error": "x"}),
        ]))


class ExcelTest(unittest.TestCase):
    """Un Excel ordenado: título y resumen arriba, cabecera destacada y fija
    con filtros, solo las columnas útiles en orden y cada dato con su formato."""

    GENERADO = datetime.datetime(2026, 9, 22, 14, 30)

    def _libro(self, *llamadas):
        import openpyxl
        datos = exportar.a_excel(exportar.tablas(list(llamadas)), generado=self.GENERADO)
        return openpyxl.load_workbook(io.BytesIO(datos))

    def test_una_hoja_por_lista(self):
        self.assertEqual(["Reservas", "Ofertas"], self._libro(RESERVAS, OFERTAS).sheetnames)

    def test_nombres_de_hoja_repetidos(self):
        self.assertEqual(["Reservas", "Reservas (2)"], self._libro(RESERVAS, RESERVAS).sheetnames)

    def test_titulo_y_resumen_arriba(self):
        hoja = self._libro(RESERVAS)["Reservas"]
        self.assertEqual("Reservas", hoja["A1"].value)
        self.assertTrue(hoja["A1"].font.bold)
        self.assertIn("2 de 463 que cumplen la búsqueda", hoja["A2"].value)
        self.assertIn("22/09/2026 14:30", hoja["A2"].value)

    def test_cabecera_destacada_fija_y_con_filtros(self):
        hoja = self._libro(RESERVAS)["Reservas"]
        self.assertTrue(hoja["A4"].font.bold)
        self.assertEqual("FF1E2B3C", hoja["A4"].fill.fgColor.rgb)
        self.assertEqual("A5", hoja.freeze_panes)
        self.assertTrue(hoja.auto_filter.ref.startswith("A4:"))

    def test_columnas_utiles_en_orden(self):
        hoja = self._libro(RESERVAS)["Reservas"]
        cabecera = [c.value for c in hoja[4] if c.value]
        self.assertEqual(["Localizador", "Titular", "Villa", "Entrada", "Salida", "Importe total (€)"],
                         cabecera)

    def test_sin_columnas_vacias(self):
        reservas = (RESERVAS[0], RESERVAS[1], dict(RESERVAS[2], reservas=[
            dict(f, fecha_anulacion=None) for f in RESERVAS[2]["reservas"]]))
        cabecera = [c.value for c in self._libro(reservas)["Reservas"][4] if c.value]
        self.assertNotIn("Anulada el", cabecera)

    def test_quita_campos_tecnicos(self):
        reservas = (RESERVAS[0], RESERVAS[1], dict(RESERVAS[2], reservas=[
            dict(RESERVAS[2]["reservas"][0], es_alto_riesgo=False, moneda_id="EUR", estado_documento="CO")]))
        cabecera = [c.value for c in self._libro(reservas)["Reservas"][4] if c.value]
        for tecnico in ("Es alto riesgo", "Moneda", "Estado del documento"):
            self.assertNotIn(tecnico, cabecera)

    def test_fechas_importes_y_si_no_con_formato(self):
        hoja = self._libro(RESERVAS, OFERTAS)["Reservas"]
        entrada, importe = hoja["D5"], hoja["F6"]
        self.assertIsInstance(entrada.value, datetime.datetime)
        self.assertEqual("dd/mm/yyyy", entrada.number_format)
        self.assertEqual(41339.25, importe.value)
        self.assertIn("€", importe.number_format)
        villas = exportar.a_excel(exportar.tablas([("buscar_propiedades", {}, {"matches": [
            {"nombre": "ADORA", "tiene_piscina_privada": True, "admite_animales": False}]})]))
        import openpyxl
        fila = [c.value for c in openpyxl.load_workbook(io.BytesIO(villas))["Villas"][5]]
        self.assertEqual(["ADORA", "Sí", "No"], [v for v in fila if v is not None])


class BotonesTest(unittest.TestCase):
    """Copiar va directo al portapapeles; los dos botones, abajo a la derecha."""

    def test_copia_directamente_y_descarga_el_excel(self):
        html = exportar.botones_html("Hola **mundo**", b"PK-excel", "abahana.xlsx")
        self.assertIn("navigator.clipboard.writeText", html)
        self.assertIn("Hola mundo", html)
        self.assertIn('download="abahana.xlsx"', html)
        self.assertIn("data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,", html)
        self.assertIn("justify-content: flex-end", html)

    def test_sin_listas_no_hay_boton_de_excel(self):
        self.assertNotIn("download=", exportar.botones_html("Hola", None, "x.xlsx"))

    def test_el_texto_no_puede_romper_la_pagina(self):
        html = exportar.botones_html("</script><script>alert(1)</script>", None, "x.xlsx")
        self.assertNotIn("</script><script>alert(1)", html)


class CopiarTest(unittest.TestCase):

    def test_tabla_para_pegar_en_una_hoja(self):
        tsv = exportar.a_tsv(exportar.tablas([RESERVAS])[0])
        lineas = tsv.splitlines()
        self.assertEqual(3, len(lineas))
        self.assertEqual("Localizador", lineas[0].split("\t")[0])
        self.assertIn("2026_3274\tFarley, Olivia\tMARACALA", lineas[1])

    def test_texto_sin_formato_para_un_correo_o_un_chat(self):
        md = ("### Reservas\n\nHay **463 reservas** con entrada en septiembre:\n\n"
              "* **2026_3274** · MARACALA\n- `2026_2550` · EL MANZANO\n\n"
              "Más en [la web](https://www.abahanavillas.com/es/).\n\n---\n")
        texto = exportar.texto_plano(md)
        self.assertNotIn("**", texto)
        self.assertNotIn("###", texto)
        self.assertNotIn("`", texto)
        self.assertNotIn("---", texto)
        self.assertIn("• 2026_3274 · MARACALA", texto)
        self.assertIn("• 2026_2550 · EL MANZANO", texto)
        self.assertIn("la web (https://www.abahanavillas.com/es/)", texto)
        self.assertTrue(texto.startswith("Reservas"))


class DespliegueTest(unittest.TestCase):

    def test_la_imagen_incluye_el_modulo_y_la_libreria(self):
        raiz = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(raiz, "Dockerfile")) as f:
            self.assertIn("COPY exportar.py .", f.read())
        with open(os.path.join(raiz, "requirements.txt")) as f:
            self.assertIn("openpyxl", f.read())


if __name__ == "__main__":
    unittest.main()
