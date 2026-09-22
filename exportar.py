"""Copiar o exportar lo que responde el agente.

Las respuestas acaban en un correo, en Google Chat, en una reunión o en una
hoja de seguimiento. De cada turno salen:
- el texto sin formato de Markdown, para pegar en un correo o un chat;
- cada lista que devolvió una herramienta, como tabla para pegar en una hoja
  (columnas separadas por tabuladores) o en un Excel con una hoja por lista.

Las listas salen de los datos de las herramientas, no del texto del modelo.
"""
from __future__ import annotations

import io
import re
from typing import Any, Iterable

import pandas as pd

# herramienta -> (clave de la lista en la respuesta, título de la hoja)
_LISTAS = {
    "buscar_propiedades": ("matches", "Villas"),
    "listar_propiedades": ("matches", "Villas"),
    "buscar_por_valoracion": ("matches", "Villas por valoración"),
    "consultar_disponibilidad": ("matches", "Villas disponibles"),
    "buscar_ofertas": ("matches", "Ofertas"),
    "alternativas_villa": ("matches", "Alternativas"),
    "consultar_reservas": ("reservas", "Reservas"),
    "resumen_reservas": ("resumen", "Resumen de reservas"),
    "consultar_precios": ("noches", "Precios por noche"),
    "calendario_villa": ("tramos", "Calendario"),
    "detalle_reserva": ("pagos", "Plan de pagos"),
}
# Claves de la respuesta con el total real, por si la lista está recortada.
_TOTALES = ("total", "total_disponibles", "total_grupos")
# Identificadores internos que no dicen nada a quien lee la hoja.
_OCULTAS = {"villa_id", "reserva_id", "cliente_id", "huesped_id", "ficha_id", "total_resultados"}

_ETIQUETAS = {
    "nombre": "Villa", "villa_nombre": "Villa", "villa": "Villa",
    "pueblo_cercano": "Pueblo", "zona": "Zona", "region": "Región", "direccion": "Dirección",
    "capacidad_pax": "Personas", "numero_habitaciones": "Habitaciones", "numero_banos": "Baños",
    "camas_totales": "Camas", "m2_habitables": "m² habitables",
    "tiene_piscina_privada": "Piscina privada", "admite_animales": "Mascotas",
    "tiene_vista_mar": "Vistas al mar", "distancia_mar_m": "Distancia al mar (m)",
    "distancia_playa_arena_m": "Distancia a playa de arena (m)",
    "rating_medio": "Valoración media",
    "precio_total": "Precio total (€)", "precio_medio_noche": "Precio medio por noche (€)",
    "margen_total": "Margen (€)", "margen_pct": "Margen (%)", "larga_estancia": "Larga estancia",
    "precio_completo": "Precio de todas las noches",
    "localizador": "Localizador", "cliente_nombre": "Titular", "titular": "Titular",
    "fecha_entrada": "Entrada", "fecha_salida": "Salida", "noches": "Noches",
    "adultos": "Adultos", "ninos": "Niños", "num_mascotas": "Mascotas",
    "estado_reserva": "Estado", "estado_documento": "Estado del documento",
    "subtipo_reserva": "Tipo", "estado_limpieza": "Limpieza",
    "fecha_anulacion": "Anulada el", "fecha_confirmacion": "Confirmada el", "fecha_pedido": "Hecha el",
    "importe_total": "Importe total (€)", "moneda_id": "Moneda",
    "dimension": "Grupo", "total_reservas": "Reservas", "importe_medio": "Importe medio (€)",
    "noches_medias": "Noches medias", "noches_totales": "Noches totales",
    "importe_por_noche": "Importe por noche (€)",
    "fecha": "Noche", "precio_venta": "Venta (€)", "precio_compra": "Compra (€)",
    "desde": "Desde", "hasta": "Hasta", "tipo_ocupacion": "Estado",
    "fecha_vencimiento": "Vencimiento", "importe_previsto": "Previsto (€)",
    "importe_pagado": "Pagado (€)", "importe_pendiente": "Pendiente (€)",
    "metodo_pago_nombre": "Forma de pago", "fecha_pago": "Pagado el",
}


def _celda(valor: Any) -> Any:
    if isinstance(valor, (list, tuple, set)):
        return ", ".join(str(v) for v in valor)
    if isinstance(valor, dict):
        return ", ".join(f"{k}: {v}" for k, v in valor.items())
    return valor


def tablas(llamadas: Iterable[tuple[str, dict, Any]]) -> list[dict]:
    """Cada lista con datos que devolvieron las herramientas del turno."""
    resultado = []
    for nombre, _, respuesta in llamadas:
        if nombre not in _LISTAS or not isinstance(respuesta, dict) or respuesta.get("error"):
            continue
        clave, titulo = _LISTAS[nombre]
        filas = [
            {k: _celda(v) for k, v in fila.items() if k not in _OCULTAS}
            for fila in respuesta.get(clave) or [] if isinstance(fila, dict)
        ]
        if not filas:
            continue
        total = next((respuesta[k] for k in _TOTALES if isinstance(respuesta.get(k), int)), None)
        resultado.append({"titulo": titulo, "filas": filas, "total": total})
    return resultado


def _tabla_legible(tabla: dict) -> pd.DataFrame:
    df = pd.DataFrame(tabla["filas"])
    return df.rename(columns={c: _ETIQUETAS.get(c, c.replace("_", " ").capitalize()) for c in df.columns})


def a_tsv(tabla: dict) -> str:
    """Columnas separadas por tabuladores: al pegar en Google Sheets o Excel
    cada dato cae en su celda."""
    return _tabla_legible(tabla).to_csv(sep="\t", index=False).strip()


def a_excel(lista_tablas: list[dict]) -> bytes:
    """Un .xlsx con una hoja por lista y las columnas en español."""
    salida = io.BytesIO()
    usados: dict[str, int] = {}
    with pd.ExcelWriter(salida, engine="openpyxl") as libro:
        for tabla in lista_tablas:
            base = tabla["titulo"][:26]
            usados[base] = usados.get(base, 0) + 1
            hoja = base if usados[base] == 1 else f"{base} ({usados[base]})"
            df = _tabla_legible(tabla)
            df.to_excel(libro, sheet_name=hoja, index=False)
            ancho = libro.sheets[hoja]
            for i, columna in enumerate(df.columns, start=1):
                largo = max([len(str(columna))] + [len(str(v)) for v in df[columna].head(200)])
                ancho.column_dimensions[ancho.cell(row=1, column=i).column_letter].width = min(largo + 2, 60)
    return salida.getvalue()


def texto_plano(markdown: str) -> str:
    """La respuesta sin la sintaxis de Markdown, para un correo o un chat."""
    lineas = []
    en_bloque_codigo = False
    for linea in (markdown or "").splitlines():
        if linea.strip().startswith("```"):
            en_bloque_codigo = not en_bloque_codigo
            continue
        if not en_bloque_codigo:
            if re.fullmatch(r"\s*([-*_])\1{2,}\s*", linea):
                continue
            linea = re.sub(r"^\s{0,3}#{1,6}\s*", "", linea)
            linea = re.sub(r"^(\s*)[*\-+]\s+", r"\1• ", linea)
            linea = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), linea)
            linea = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"\1", linea)
            linea = re.sub(r"`([^`]*)`", r"\1", linea)
            linea = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", linea)
        lineas.append(linea.rstrip())
    texto = "\n".join(lineas)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()
