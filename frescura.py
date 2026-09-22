"""Cuándo se actualizó cada dato que usa una respuesta.

Se muestra el último cambio registrado en Etendo y cuándo se cargó en las
tablas del agente. Lo primero es lo que importa: con la copia de pre-control
las cargas nocturnas funcionan, pero el último cambio de reservas puede ser de
hace días, y "actualizado hoy" daría una confianza que el dato no merece.
"""
from __future__ import annotations

import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

_MADRID = ZoneInfo("Europe/Madrid")
_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

# dominio -> (tabla de silver, rótulo, participio concordado)
_DOMINIOS = {
    "villas": ("stg_etendo_Villa", "Villas", "cargadas"),
    "disponibilidad": ("stg_etendo_Ocupacion", "Disponibilidad", "cargada"),
    "precios": ("stg_etendo_TarifaDia", "Precios", "cargados"),
    "reservas": ("stg_etendo_Reserva", "Reservas", "cargadas"),
}
_HERRAMIENTAS = {
    "buscar_propiedades": ("villas",),
    "listar_propiedades": ("villas",),
    "buscar_por_valoracion": ("villas",),
    "obtener_detalle_propiedad": ("villas",),
    "consultar_disponibilidad": ("villas", "disponibilidad"),
    "calendario_villa": ("disponibilidad",),
    "consultar_precios": ("precios",),
    "buscar_ofertas": ("villas", "disponibilidad", "precios"),
    "alternativas_villa": ("villas", "disponibilidad", "precios"),
    "consultar_reservas": ("reservas",),
    "resumen_reservas": ("reservas",),
    "detalle_reserva": ("reservas",),
}
_HERRAMIENTAS_WEB = {"consultar_web", "buscar_pagina_web", "buscar_internet"}
# En producción reservas y ocupación cambian a diario: si no, algo va mal
# aguas arriba. Villas y precios cambian poco y un aviso sería ruido.
_AVISAR_SI_PARADO = {"reservas", "disponibilidad"}
_PARADO = datetime.timedelta(hours=24)


def dominios(llamadas: Iterable[tuple[str, dict, Any]]) -> list[str]:
    """Qué datos de Etendo ha usado el turno, en orden fijo y sin repetir."""
    usados = {d for nombre, _, _ in llamadas for d in _HERRAMIENTAS.get(nombre, ())}
    return [d for d in _DOMINIOS if d in usados]


def uso_web(llamadas: Iterable[tuple[str, dict, Any]]) -> bool:
    return any(nombre in _HERRAMIENTAS_WEB for nombre, _, _ in llamadas)


def _cuando(momento: datetime.datetime, ahora: datetime.datetime) -> str:
    local = momento.astimezone(_MADRID)
    hora = local.strftime("%H:%M")
    dias = (ahora.astimezone(_MADRID).date() - local.date()).days
    if dias == 0:
        return f"hoy a las {hora}"
    if dias == 1:
        return f"ayer a las {hora}"
    return f"el {_DIAS[local.weekday()]} {local.day} {_MESES[local.month - 1]} {hora}"


def lineas(usados: list[str], datos: dict[str, dict], ahora: datetime.datetime,
           web: bool = False) -> list[str]:
    """Una línea por tipo de dato; sin datos de un tipo, no se inventa nada."""
    resultado = []
    for dominio in usados:
        info = datos.get(dominio) or {}
        cambio, cargado = info.get("ultimo_cambio"), info.get("cargado")
        if not cambio and not cargado:
            continue
        _, rotulo, participio = _DOMINIOS[dominio]
        partes = []
        if cambio:
            partes.append(f"último cambio en Etendo {_cuando(cambio, ahora)}")
        if cargado:
            partes.append(f"{participio} {_cuando(cargado, ahora)}")
        linea = f"{rotulo}: " + " · ".join(partes)
        if cambio and dominio in _AVISAR_SI_PARADO and ahora - cambio > _PARADO:
            linea += " · puede no estar al día"
        resultado.append(linea)
    if web:
        resultado.append(f"Web e internet: consultado {_cuando(ahora, ahora)}")
    return resultado


def consultar(bq, proyecto: str = "abahanaweb", dataset: str = "silver_clean") -> dict[str, dict]:
    """Último cambio en Etendo y hora de carga de cada tipo de dato.

    Una sola consulta que lee solo la columna de fecha de cada tabla (la de
    ocupación, la mayor, son unos 9 MB) y los metadatos de las tablas.
    """
    from google.cloud import bigquery

    sql = "\nUNION ALL\n".join(
        f"SELECT '{dominio}' AS dominio, MAX(fecha_actualizacion) AS ultimo_cambio "
        f"FROM `{proyecto}.{dataset}.{tabla}`"
        for dominio, (tabla, _, _) in _DOMINIOS.items()
    )
    filas = bq.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=200 * 1024 * 1024)).result()
    datos: dict[str, dict] = {d: {} for d in _DOMINIOS}
    for fila in filas:
        datos[fila.dominio]["ultimo_cambio"] = fila.ultimo_cambio
    for dominio, (tabla, _, _) in _DOMINIOS.items():
        datos[dominio]["cargado"] = bq.get_table(f"{proyecto}.{dataset}.{tabla}").modified
    return datos
