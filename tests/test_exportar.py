"""Copiar o exportar lo que responde el agente: texto para un correo o un chat,
listas para una hoja de seguimiento. Sale de los datos de las herramientas, no
del texto del modelo."""

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

    def test_una_hoja_por_lista_con_columnas_en_espanol(self):
        datos = exportar.a_excel(exportar.tablas([RESERVAS, OFERTAS]))
        hojas = pd.read_excel(io.BytesIO(datos), sheet_name=None)
        self.assertEqual(["Reservas", "Ofertas"], list(hojas))
        self.assertIn("Localizador", hojas["Reservas"].columns)
        self.assertIn("Titular", hojas["Reservas"].columns)
        self.assertEqual(41339.25, hojas["Reservas"]["Importe total (€)"][1])

    def test_nombres_de_hoja_repetidos(self):
        datos = exportar.a_excel(exportar.tablas([RESERVAS, RESERVAS]))
        self.assertEqual(["Reservas", "Reservas (2)"], list(pd.read_excel(io.BytesIO(datos), sheet_name=None)))


class CopiarTest(unittest.TestCase):

    def test_tabla_para_pegar_en_una_hoja(self):
        tsv = exportar.a_tsv(exportar.tablas([RESERVAS])[0])
        lineas = tsv.splitlines()
        self.assertEqual(3, len(lineas))
        self.assertEqual("Localizador", lineas[0].split("\t")[0])
        self.assertIn("2026_3274\tMARACALA\tFarley, Olivia", lineas[1])

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
