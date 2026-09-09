"""
Agente de Abahana Villas — responde preguntas sobre villas y reservas
usando Google ADK + BigQuery (capa silver_clean).

Roles disponibles:
  cliente  — catálogo público, búsqueda y valoraciones
  interno  — todo lo anterior + amenidades extendidas y ficha completa
  admin    — igual que interno, preparado para herramientas sensibles futuras
"""

import asyncio
import concurrent.futures
import datetime
import functools
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google import genai
from google.adk.agents import Agent
from google.cloud import bigquery
from google.genai import types as genai_types

log = logging.getLogger("agente-villas")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
DATASET = "silver_clean"
TABLA_VILLA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Villa`"
TABLA_RESERVAS = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Reserva`"
TABLA_PLANTA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Planta`"
TABLA_BANIO = f"`{PROJECT_ID}.{DATASET}.stg_etendo_OV_Banios`"
# Estancia = cada habitación de la villa (dormitorio, salón, cocina...). Es la
# única fuente con el desglose real de camas; se enlaza por planta_id, igual
# que los baños: Villa -> Planta -> Estancia.
TABLA_ESTANCIA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Estancia`"
# Ocupacion es el calendario diario por villa. TarifaDia cuelga de ella por
# ocupacion_id: no tiene fecha propia, la fecha la pone Ocupacion.
TABLA_OCUPACION = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Ocupacion`"
TABLA_TARIFA_DIA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_TarifaDia`"

_CONCEPTO_VILLA = "PrecioVilla"      # el resto de conceptos son extras
_MAX_DIAS_CONSULTA = 92
# Ocupacion (1 M de filas) y TarifaDia (1,4 M) son las tablas de grano diario.
# Ninguna está particionada por fecha ni agrupada por villa, así que cualquier
# consulta las escanea enteras: ~60 MB y ~190 MB respectivamente, por encima
# del tope general. Se les da un tope propio en vez de subirlo para todas las
# herramientas. Particionar Ocupacion por fecha y agrupar TarifaDia por
# ocupacion_id en Dataform dejaría esto en una fracción.
_BILLING_CAP_DIARIO = 500 * 1024 * 1024
TABLA_FICHA_TECNICA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_opxdes_ficha_tecnica`"

# stg_etendo_Villa trae filas duplicadas por villa_id (misma villa varias veces).
# Este filtro se aplica siempre para quedarnos con una sola fila por villa.
_DEDUP_VILLA = (
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY villa_id ORDER BY fecha_actualizacion DESC) = 1"
)

# El filtro activo/visible va SIEMPRE dentro del CTE, antes del QUALIFY: al
# revés, una villa cuya fila más reciente esté inactiva desaparece entera en
# lugar de resolverse por su última fila vigente. Definirlo una sola vez evita
# que el catálogo y la búsqueda apliquen criterios distintos.
_CTE_VILLAS_VIGENTES = f"""
        villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            WHERE es_activo = TRUE AND es_visible = TRUE
            {_DEDUP_VILLA}
        )"""

# Las únicas camas de verdad del lake: Villa.capacidad_camas y
# ficha_tecnica.total_camas son en realidad recuentos de habitaciones (191/204
# y 197/204 coinciden con el número de dormitorios, no con el de camas).
_CTE_CAMAS = f"""
        camas AS (
            SELECT
                p.villa_id,
                SUM(COALESCE(e.num_camas_dobles, 0)
                    + COALESCE(e.num_camas_king_size, 0)
                    + COALESCE(e.num_camas_simples, 0)
                    + COALESCE(e.num_literas, 0)
                    + COALESCE(e.num_camas_nido, 0)
                    + COALESCE(e.num_camas_partidas, 0)) AS camas_totales,
                COUNTIF(e.tipo_estancia = 'dormitorio') AS dormitorios
            FROM {TABLA_PLANTA} p
            JOIN {TABLA_ESTANCIA} e
                ON e.planta_id = p.planta_id AND e.es_activo = TRUE
            GROUP BY p.villa_id
        )"""

_CTE_FICHA = f"""
        ficha AS (
            SELECT
                propiedad_codigo,
                ANY_VALUE(tiene_internet) AS tiene_internet,
                ANY_VALUE(tiene_aire_salon) AS tiene_aire_acondicionado,
                ANY_VALUE(tiene_lavadora) AS tiene_lavadora,
                ANY_VALUE(tiene_lavavajillas) AS tiene_lavavajillas
            FROM {TABLA_FICHA_TECNICA}
            WHERE propiedad_codigo IS NOT NULL
            GROUP BY propiedad_codigo
        )"""

# Proyección corta a propósito: todo lo que devuelve una herramienta entra en
# el contexto del modelo. Con 891 propiedades, arrastrar descripciones largas
# multiplica el coste de cada consulta. La ficha completa de una villa concreta
# se pide con obtener_detalle_propiedad.
_COLUMNAS_RESUMEN = """
            v.nombre, v.tipovilla_nombre_comercial,
            v.capacidad_pax, v.numero_banos, v.m2_habitables,
            COALESCE(c.camas_totales, 0) AS camas_totales,
            v.tiene_piscina_privada, v.admite_animales,
            f.tiene_internet, f.tiene_aire_acondicionado,
            f.tiene_lavadora, f.tiene_lavavajillas,
            v.pueblo_cercano, v.zona,
            ROUND((COALESCE(v.rating_exterior, 0) + COALESCE(v.rating_interior, 0) +
                   COALESCE(v.rating_vistas, 0)) /
                  NULLIF((CASE WHEN v.rating_exterior IS NOT NULL THEN 1 ELSE 0 END +
                          CASE WHEN v.rating_interior IS NOT NULL THEN 1 ELSE 0 END +
                          CASE WHEN v.rating_vistas IS NOT NULL THEN 1 ELSE 0 END), 0),
                  2) AS rating_medio,
            COUNT(*) OVER () AS total_resultados"""


def _separar_total(rows) -> tuple[list[dict], int]:
    """Extrae el total exacto y lo saca de cada fila.

    Se cuenta con COUNT(*) OVER () para no cortar los resultados y aun así
    poder decir cuántos hay: un LIMIT fijo ocultaba coincidencias en silencio.
    """
    matches = [_row_to_dict(r) for r in rows]
    total = matches[0].pop("total_resultados", len(matches)) if matches else 0
    for m in matches[1:]:
        m.pop("total_resultados", None)
    return matches, total

_bq = bigquery.Client(project=PROJECT_ID, location="EU")
_BILLING_CAP = 50 * 1024 * 1024  # 50 MB — tablas silver_clean materializadas
_TIMEZONE = ZoneInfo("Europe/Madrid")
# OJO: estos valores NO son etiquetas para el usuario. Se comparan contra el
# contenido real de r.estado_reserva / r.estado_documento en BigQuery, donde el
# mismo estado aparece unas veces como código y otras escrito. Por eso cada
# código acepta varias grafías, igual que ya se hacía con las cancelaciones.
_ESTADOS_RESERVA = {
    "RE": ("RESERVA",),
    "PE": ("PERDIDA",),
    "CA": ("CANCELACION", "CANCELADA"),
    "NS": ("NOSHOW", "NO SHOW"),
    "PR": ("PRERESERVA",),
    "BO": ("BLOQUEADA", "BORRADOR"),
}
_ESTADOS_DOCUMENTO = {
    "CO": ("CONFIRMADA", "COMPLETADA"),
    "CL": ("CERRADA",),
    "DR": ("BORRADOR",),
    "VO": ("ANULADA", "ANULADO"),
}


def _filtro_estado(columna: str, prefijo: str, codigo: str,
                   equivalencias: dict[str, tuple[str, ...]],
                   params: list) -> str:
    """Condición que acepta el código y cualquiera de sus grafías."""
    codigo = codigo.strip().upper()
    valores = list(dict.fromkeys((codigo, *equivalencias.get(codigo, ()))))
    marcadores = []
    for i, valor in enumerate(valores):
        nombre = f"{prefijo}_{i}"
        marcadores.append(f"@{nombre}")
        params.append(bigquery.ScalarQueryParameter(nombre, "STRING", valor))
    return f"UPPER({columna}) IN ({', '.join(marcadores)})"


@functools.lru_cache(maxsize=1)
def _columna_habitaciones() -> str:
    """Columna real de habitaciones en stg_etendo_Villa.

    Dataform la renombra de `capacidad_camas` a `numero_habitaciones` (el
    origen es ovjchHabTotales: habitaciones, no camas). Hasta que el pipeline
    se ejecute, la tabla conserva el nombre viejo, así que se resuelve una vez
    en caliente en lugar de fijarlo y arriesgar una caída durante la
    transición. Cuando el renombrado esté desplegado, esto puede fijarse.
    """
    try:
        rows = list(_bq.query(
            f"""
            SELECT column_name
            FROM `{PROJECT_ID}.{DATASET}.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name = 'stg_etendo_Villa'
              AND column_name IN ('numero_habitaciones', 'capacidad_camas')
            """,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_BILLING_CAP),
        ).result())
        nombres = {r["column_name"] for r in rows}
    except Exception:
        log.warning("No se pudo resolver la columna de habitaciones", exc_info=True)
        return "numero_habitaciones"
    return (
        "numero_habitaciones" if "numero_habitaciones" in nombres
        else "capacidad_camas"
    )


def _row_to_dict(row) -> dict:
    return {
        k: v.isoformat() if isinstance(v, (datetime.date, datetime.datetime)) else v
        for k, v in row.items()
    }


def _ahora_local() -> datetime.datetime:
    """Fecha y hora oficial usada por el agente (España peninsular)."""
    return datetime.datetime.now(_TIMEZONE)


def _parse_iso_date(value: str, field_name: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} debe tener formato YYYY-MM-DD.") from exc


_VERTEX_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "europe-west1")
_genai_client: genai.Client | None = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(
            vertexai=os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() == "true",
            project=PROJECT_ID,
            location=_VERTEX_LOCATION,
        )
    return _genai_client


# ---------------------------------------------------------------------------
# Herramientas
# ---------------------------------------------------------------------------

def obtener_fecha_hora_actual() -> dict[str, Any]:
    """Devuelve la fecha y hora actual en la zona del agente.

    Usa esta herramienta para interpretar referencias relativas como "hoy",
    "mañana", "este fin de semana", "este mes" o "dentro de una semana".
    La zona horaria oficial es Europe/Madrid.
    """
    ahora = _ahora_local()
    return {
        "fecha": ahora.date().isoformat(),
        "hora": ahora.strftime("%H:%M:%S"),
        "fecha_hora": ahora.isoformat(),
        "zona_horaria": "Europe/Madrid",
    }


def listar_propiedades() -> dict[str, Any]:
    """Devuelve el catálogo completo de propiedades activas y visibles.

    Usa cuando el usuario pida ver el catálogo o todas las villas sin filtros.
    Devuelve el total exacto en 'total'; no recorta resultados.
    """
    return buscar_propiedades()


def buscar_propiedades(
    ubicacion: str | None = None,
    zona: str | None = None,
    capacidad_min: int | None = None,
    habitaciones_min: int | None = None,
    camas_min: int | None = None,
    banos_min: int | None = None,
    metros_habitables_min: int | None = None,
    piscina: bool | None = None,
    admite_animales: bool | None = None,
    internet: bool | None = None,
    aire_acondicionado: bool | None = None,
    lavadora: bool | None = None,
    lavavajillas: bool | None = None,
    texto: str | None = None,
) -> dict[str, Any]:
    """Busca propiedades aplicando cualquier combinación de filtros.

    Usa esta función para todos los criterios de búsqueda: ubicación,
    capacidad, camas, baños, metros y amenidades disponibles.

    Args:
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira, Benidorm, Dénia…).
        zona: Zona geográfica (Costa Blanca Norte, Sur…).
        capacidad_min: Número mínimo de personas.
        habitaciones_min: Número mínimo de habitaciones (dormitorios).
        camas_min: Número mínimo de camas reales, sumando las de cada
            dormitorio. No confundir con habitaciones_min: una habitación
            puede tener más de una cama.
        banos_min: Número mínimo de baños.
        metros_habitables_min: Metros habitables mínimos.
        piscina: True para exigir piscina privada.
        admite_animales: True para propiedades que admiten mascotas.
        internet: True para exigir wifi/internet.
        aire_acondicionado: True para exigir aire acondicionado (dato registrado
            a nivel de salón, puede no cubrir el resto de la villa).
        lavadora: True para exigir lavadora.
        lavavajillas: True para exigir lavavajillas.
        texto: Busca en nombre y tipo de villa.

    Returns:
        Diccionario con 'matches' (todas las coincidencias), 'count' y 'total'.
    """
    # activo/visible se aplican dentro del CTE, antes de deduplicar.
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if ubicacion:
        conditions.append("LOWER(v.pueblo_cercano) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter("ubicacion", "STRING", f"%{ubicacion.strip()}%"))

    if zona:
        conditions.append("LOWER(v.zona) LIKE LOWER(@zona)")
        params.append(bigquery.ScalarQueryParameter("zona", "STRING", f"%{zona.strip()}%"))

    if capacidad_min is not None:
        conditions.append("v.capacidad_pax >= @capacidad_min")
        params.append(bigquery.ScalarQueryParameter("capacidad_min", "INT64", capacidad_min))

    if habitaciones_min is not None:
        conditions.append(
            f"v.{_columna_habitaciones()} >= @habitaciones_min"
        )
        params.append(bigquery.ScalarQueryParameter(
            "habitaciones_min", "INT64", habitaciones_min
        ))

    if camas_min is not None:
        conditions.append("c.camas_totales >= @camas_min")
        params.append(bigquery.ScalarQueryParameter("camas_min", "INT64", camas_min))

    if banos_min is not None:
        conditions.append("v.numero_banos >= @banos_min")
        params.append(bigquery.ScalarQueryParameter("banos_min", "INT64", banos_min))

    if metros_habitables_min is not None:
        conditions.append("v.m2_habitables >= @metros_habitables_min")
        params.append(bigquery.ScalarQueryParameter("metros_habitables_min", "INT64", int(metros_habitables_min)))

    if piscina is not None:
        conditions.append(f"v.tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")

    if admite_animales is not None:
        conditions.append(f"v.admite_animales = {'TRUE' if admite_animales else 'FALSE'}")

    if internet is not None:
        conditions.append(
            f"f.tiene_internet = TRUE"
            if internet
            else f"COALESCE(f.tiene_internet, FALSE) = FALSE"
        )

    if aire_acondicionado is not None:
        conditions.append(
            f"f.tiene_aire_acondicionado = TRUE"
            if aire_acondicionado
            else f"COALESCE(f.tiene_aire_acondicionado, FALSE) = FALSE"
        )

    if lavadora is not None:
        conditions.append(
            f"f.tiene_lavadora = TRUE"
            if lavadora
            else f"COALESCE(f.tiene_lavadora, FALSE) = FALSE"
        )

    if lavavajillas is not None:
        conditions.append(
            f"f.tiene_lavavajillas = TRUE"
            if lavavajillas
            else f"COALESCE(f.tiene_lavavajillas, FALSE) = FALSE"
        )

    if texto:
        conditions.append(
            "(LOWER(v.nombre) LIKE LOWER(@texto) OR LOWER(v.tipovilla_descripcion) LIKE LOWER(@texto))"
        )
        params.append(bigquery.ScalarQueryParameter("texto", "STRING", f"%{texto.strip()}%"))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        WITH{_CTE_VILLAS_VIGENTES},{_CTE_FICHA},{_CTE_CAMAS}
        SELECT
            v.{_columna_habitaciones()} AS numero_habitaciones,{_COLUMNAS_RESUMEN}
        FROM villa_dedup v
        LEFT JOIN ficha f ON SAFE_CAST(v.codigo_busqueda AS INT64) = f.propiedad_codigo
        LEFT JOIN camas c ON c.villa_id = v.villa_id
        {where}
        ORDER BY v.nombre
    """

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as e:
        log.exception("buscar_propiedades: error en BigQuery")
        return {"matches": [], "count": 0, "total": 0, "error": str(e)}

    matches, total = _separar_total(rows)
    return {"matches": matches, "count": len(matches), "total": total}


def buscar_por_valoracion(
    rating_min: float | None = None,
    ubicacion: str | None = None,
    capacidad_min: int | None = None,
    piscina: bool | None = None,
) -> dict[str, Any]:
    """Busca propiedades por valoración media mínima.

    Usa cuando el usuario pida villas bien valoradas, con buena puntuación,
    las mejor valoradas, o mencione valoraciones/ratings.
    La valoración media se calcula sobre las tres valoraciones que existen
    en el catálogo: exterior, interior y vistas.

    Args:
        rating_min: Puntuación media mínima (escala 1-6). Si no se especifica, ordena por rating desc.
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira…).
        capacidad_min: Número mínimo de personas.
        piscina: True para exigir piscina privada.

    Returns:
        Diccionario con 'matches' (todas las coincidencias), 'count' y 'total'.
    """
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if ubicacion:
        conditions.append("LOWER(pueblo_cercano) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter("ubicacion", "STRING", f"%{ubicacion.strip()}%"))

    if capacidad_min is not None:
        conditions.append("capacidad_pax >= @capacidad_min")
        params.append(bigquery.ScalarQueryParameter("capacidad_min", "INT64", capacidad_min))

    if piscina is not None:
        conditions.append(f"tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")

    if rating_min is not None:
        conditions.append("rating_medio >= @rating_min")
        params.append(bigquery.ScalarQueryParameter("rating_min", "FLOAT64", rating_min))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        WITH base AS (
            SELECT
                nombre, pueblo_cercano, zona, region,
                capacidad_pax, {_columna_habitaciones()} AS numero_habitaciones,
                numero_banos,
                tiene_piscina_privada, admite_animales,
                rating_exterior, rating_interior, rating_vistas,
                ROUND(
                    (COALESCE(rating_exterior, 0) + COALESCE(rating_interior, 0) +
                     COALESCE(rating_vistas, 0)) /
                    NULLIF(
                        (CASE WHEN rating_exterior IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN rating_interior IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN rating_vistas IS NOT NULL THEN 1 ELSE 0 END), 0
                    ), 2
                ) AS rating_medio
            FROM {TABLA_VILLA}
            WHERE es_activo = TRUE AND es_visible = TRUE
            {_DEDUP_VILLA}
        )
        SELECT *, COUNT(*) OVER () AS total_resultados
        FROM base
        {where}
        ORDER BY rating_medio DESC
    """

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as e:
        log.exception("buscar_por_valoracion: error en BigQuery")
        return {"matches": [], "count": 0, "total": 0, "error": str(e)}

    matches, total = _separar_total(rows)
    return {"matches": matches, "count": len(matches), "total": total}


# Campos que se suman de las plantas para dar el total de la villa. El nombre
# del total coincide con el de la planta salvo en los baños, donde se conserva
# `banios_con_detalle` para no romper a quien ya lo consumía.
_TOTALES_PLANTA = {
    "banios_con_detalle": "banios",
    "banios_con_banera": "banios_con_banera",
    "banios_con_ducha": "banios_con_ducha",
    "banios_con_jacuzzi": "banios_con_jacuzzi",
    "banios_con_bide": "banios_con_bide",
    "banios_ensuite": "banios_ensuite",
    "dormitorios": "dormitorios",
    "camas_totales": "camas_totales",
    "camas_dobles": "camas_dobles",
    "camas_king_size": "camas_king_size",
    "camas_simples": "camas_simples",
    "literas": "literas",
    "camas_nido": "camas_nido",
    "camas_partidas": "camas_partidas",
    "dormitorios_en_suite": "dormitorios_en_suite",
    "estancias_con_sofacama": "estancias_con_sofacama",
}


def _totales_de_plantas(plantas: list[dict[str, Any]]) -> dict[str, int]:
    return {
        total: sum(p.get(campo) or 0 for p in plantas)
        for total, campo in _TOTALES_PLANTA.items()
    }


def obtener_detalle_propiedad(nombre: str) -> dict[str, Any]:
    """Devuelve la ficha completa de una propiedad específica.

    Usa cuando el usuario pregunte por una villa concreta por su nombre,
    quiera más información sobre una propiedad, o pida ver todos los detalles.
    Incluye dirección completa, coordenadas, métricas de habitaciones, ratings,
    amenidades (internet, aire acondicionado, lavadora, lavavajillas), el
    desglose real de los baños (cuántos tienen bañera, ducha, jacuzzi, bidé
    o son en-suite) y el desglose real de los dormitorios: cuántos hay,
    cuántas camas en total y de qué tipo (dobles, king size, simples, literas,
    nido, partidas), cuántos dormitorios son en-suite y cuántas estancias
    tienen sofá cama.

    Devuelve además `plantas`: la misma información **planta a planta**, con el
    nombre y número de cada una, sus baños y sus dormitorios. Úsalo para
    preguntas del tipo "cuántos baños hay en cada planta" o "qué hay en el
    sótano". Los totales de la villa son la suma de sus plantas.

    Args:
        nombre: Nombre o parte del nombre de la propiedad.

    Returns:
        Diccionario con 'matches' (lista de propiedades encontradas) y 'count'.
    """
    nombre_limpio = nombre.strip()
    if nombre_limpio.lower().startswith("villa "):
        nombre_limpio = nombre_limpio[6:].strip()
    params = [
        bigquery.ScalarQueryParameter(
            "nombre", "STRING", f"%{nombre_limpio}%"
        ),
        bigquery.ScalarQueryParameter(
            "nombre_exacto", "STRING", nombre_limpio
        ),
    ]
    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            WHERE LOWER(nombre) LIKE LOWER(@nombre) AND es_activo = TRUE
            {_DEDUP_VILLA}
        )
        SELECT
            v.villa_id,
            v.codigo_busqueda,
            v.nombre, v.tipovilla_nombre_comercial, v.tipovilla_descripcion,
            v.anio_construccion, v.m2_parcela, v.m2_habitables,
            v.capacidad_pax, v.{_columna_habitaciones()} AS numero_habitaciones,
            v.numero_banos, v.numero_plantas,
            v.tiene_piscina_privada, v.tiene_piscina_comun, v.piscina_climatizada,
            v.tiene_jardin, v.tiene_garaje, v.admite_animales,
            v.pueblo_cercano, v.zona, v.region, v.direccion,
            v.latitud, v.longitud,
            v.rating_exterior, v.rating_interior, v.rating_vistas,
            v.propietario_nombre, v.es_recomendada, v.es_novedad
        FROM villa_dedup v
        ORDER BY
            CASE WHEN UPPER(v.nombre) = UPPER(@nombre_exacto) THEN 0 ELSE 1 END,
            v.nombre
        LIMIT 5
    """

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as e:
        log.exception("obtener_detalle_propiedad: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [_row_to_dict(r) for r in rows]
    if not matches:
        return {"matches": [], "count": 0}

    # Las amenidades viven en otra tabla y no deben impedir reconocer una villa.
    codigos = [
        int(match["codigo_busqueda"])
        for match in matches
        if str(match.get("codigo_busqueda") or "").isdigit()
    ]
    ficha_por_codigo: dict[int, dict[str, Any]] = {}
    detalle_amenidades_disponible = True
    if codigos:
        query_ficha = f"""
            SELECT
                propiedad_codigo,
                ANY_VALUE(tiene_internet) AS tiene_internet,
                ANY_VALUE(tiene_aire_salon) AS tiene_aire_acondicionado,
                ANY_VALUE(tiene_lavadora) AS tiene_lavadora,
                ANY_VALUE(tiene_lavavajillas) AS tiene_lavavajillas
            FROM {TABLA_FICHA_TECNICA}
            WHERE propiedad_codigo IN UNNEST(@codigos)
            GROUP BY propiedad_codigo
        """
        try:
            rows_ficha = list(_bq.query(
                query_ficha,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ArrayQueryParameter(
                            "codigos", "INT64", codigos
                        )
                    ],
                    maximum_bytes_billed=_BILLING_CAP,
                ),
            ).result())
            ficha_por_codigo = {
                int(row["propiedad_codigo"]): _row_to_dict(row)
                for row in rows_ficha
            }
        except Exception:
            detalle_amenidades_disponible = False
            log.warning(
                "obtener_detalle_propiedad: amenidades no disponibles",
                exc_info=True,
            )

    # El desglose es enriquecimiento opcional: se consulta aparte para que una
    # ausencia o un cambio en Planta/Banios/Estancia no inutilice la ficha.
    villa_ids = [match["villa_id"] for match in matches]

    # Baños y estancias cuelgan de una planta, no de la villa. Se consulta con
    # ese grano y los totales de villa se derivan sumando, para que el desglose
    # por planta y el total no puedan descuadrar entre sí.
    query_plantas = f"""
        WITH banios AS (
            SELECT
                planta_id,
                COUNT(*) AS banios,
                COUNTIF(tiene_baniera OR tiene_baniera_suelta) AS banios_con_banera,
                COUNTIF(
                    tiene_ducha OR tiene_ducha_plato OR tiene_ducha_obra
                    OR tiene_ducha_hidromasaje
                ) AS banios_con_ducha,
                COUNTIF(tiene_jacuzzi OR tiene_jacuzzi_baniera) AS banios_con_jacuzzi,
                COUNTIF(tiene_bide) AS banios_con_bide,
                COUNTIF(es_en_suite) AS banios_ensuite
            FROM {TABLA_BANIO}
            WHERE es_activo = TRUE
            GROUP BY planta_id
        ),
        estancias AS (
            SELECT
                planta_id,
                COUNTIF(tipo_estancia = 'dormitorio') AS dormitorios,
                SUM(COALESCE(num_camas_dobles, 0)
                    + COALESCE(num_camas_king_size, 0)
                    + COALESCE(num_camas_simples, 0)
                    + COALESCE(num_literas, 0)
                    + COALESCE(num_camas_nido, 0)
                    + COALESCE(num_camas_partidas, 0)) AS camas_totales,
                SUM(COALESCE(num_camas_dobles, 0)) AS camas_dobles,
                SUM(COALESCE(num_camas_king_size, 0)) AS camas_king_size,
                SUM(COALESCE(num_camas_simples, 0)) AS camas_simples,
                SUM(COALESCE(num_literas, 0)) AS literas,
                SUM(COALESCE(num_camas_nido, 0)) AS camas_nido,
                SUM(COALESCE(num_camas_partidas, 0)) AS camas_partidas,
                COUNTIF(es_en_suite) AS dormitorios_en_suite,
                COUNTIF(tiene_sofacama) AS estancias_con_sofacama
            FROM {TABLA_ESTANCIA}
            WHERE es_activo = TRUE
            GROUP BY planta_id
        )
        SELECT
            p.villa_id,
            p.numero_planta,
            p.nombre AS planta,
            p.tiene_jacuzzi AS planta_tiene_jacuzzi,
            p.tiene_sauna AS planta_tiene_sauna,
            COALESCE(b.banios, 0) AS banios,
            COALESCE(b.banios_con_banera, 0) AS banios_con_banera,
            COALESCE(b.banios_con_ducha, 0) AS banios_con_ducha,
            COALESCE(b.banios_con_jacuzzi, 0) AS banios_con_jacuzzi,
            COALESCE(b.banios_con_bide, 0) AS banios_con_bide,
            COALESCE(b.banios_ensuite, 0) AS banios_ensuite,
            COALESCE(e.dormitorios, 0) AS dormitorios,
            COALESCE(e.camas_totales, 0) AS camas_totales,
            COALESCE(e.camas_dobles, 0) AS camas_dobles,
            COALESCE(e.camas_king_size, 0) AS camas_king_size,
            COALESCE(e.camas_simples, 0) AS camas_simples,
            COALESCE(e.literas, 0) AS literas,
            COALESCE(e.camas_nido, 0) AS camas_nido,
            COALESCE(e.camas_partidas, 0) AS camas_partidas,
            COALESCE(e.dormitorios_en_suite, 0) AS dormitorios_en_suite,
            COALESCE(e.estancias_con_sofacama, 0) AS estancias_con_sofacama
        FROM {TABLA_PLANTA} p
        LEFT JOIN banios b ON b.planta_id = p.planta_id
        LEFT JOIN estancias e ON e.planta_id = p.planta_id
        WHERE p.villa_id IN UNNEST(@villa_ids) AND p.es_activo = TRUE
        ORDER BY p.villa_id, p.numero_planta
    """
    plantas_por_villa: dict[str, list[dict[str, Any]]] = {}
    detalle_plantas_disponible = True
    try:
        rows_plantas = list(_bq.query(
            query_plantas,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("villa_ids", "STRING", villa_ids)
                ],
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
        for row in rows_plantas:
            fila = _row_to_dict(row)
            plantas_por_villa.setdefault(fila.pop("villa_id"), []).append(fila)
    except Exception:
        detalle_plantas_disponible = False
        log.warning(
            "obtener_detalle_propiedad: desglose por planta no disponible",
            exc_info=True,
        )

    for match in matches:
        villa_id = match.pop("villa_id")
        codigo_raw = match.pop("codigo_busqueda", None)
        codigo = int(codigo_raw) if str(codigo_raw or "").isdigit() else None
        detalle_ficha = ficha_por_codigo.get(codigo, {}) if codigo else {}
        detalle_ficha.pop("propiedad_codigo", None)
        match.update(detalle_ficha)
        plantas = plantas_por_villa.get(villa_id)
        if plantas:
            match["plantas"] = plantas
            match.update(_totales_de_plantas(plantas))

    return {
        "matches": matches,
        "count": len(matches),
        "detalle_amenidades_disponible": detalle_amenidades_disponible,
        "detalle_banios_disponible": detalle_plantas_disponible,
        "detalle_dormitorios_disponible": detalle_plantas_disponible,
    }


_DOMINIO_WEB = "abahanavillas.com"

# Nada de esto hace falta para extraer texto, y cada uno es una descarga más
# dentro de un contenedor de 2 GiB que además arranca un Chromium por visita.
_RECURSOS_IGNORADOS = {"image", "media", "font", "stylesheet"}

_CACHE_WEB: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_WEB_TTL = 3600.0
_CACHE_WEB_MAX = 32


def _url_permitida(url: str) -> bool:
    """Comprueba que la URL es realmente del sitio de Abahana.

    Un `in` sobre netloc no vale: `abahanavillas.com.atacante.io` lo pasaría,
    y detrás hay un navegador de verdad haciendo la petición desde Cloud Run.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == _DOMINIO_WEB or host.endswith(f".{_DOMINIO_WEB}")


async def _fetch_con_playwright(url: str) -> tuple[int, str]:
    from playwright.async_api import async_playwright

    async def _filtrar(ruta):
        if ruta.request.resource_type in _RECURSOS_IGNORADOS:
            await ruta.abort()
        else:
            await ruta.continue_()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = await browser.new_page(locale="es-ES")
            await page.route("**/*", _filtrar)
            respuesta = await page.goto(
                url, wait_until="domcontentloaded", timeout=20000
            )
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
            estado = respuesta.status if respuesta else 0
            return estado, await page.content()
        finally:
            await browser.close()


def _playwright_en_hilo(url: str) -> tuple[int, str]:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch_con_playwright(url))
    finally:
        loop.close()


def consultar_web(url: str) -> dict[str, Any]:
    """Obtiene el contenido de una página de la web de Abahana Villas.

    Usa esta herramienta cuando el usuario pregunte por información de la
    empresa: política de privacidad, aviso legal, condiciones de alquiler,
    contacto, oficinas, destinos, o cualquier otra página del sitio web.

    El agente decide qué URL visitar según lo que el usuario pregunte.
    La URL base del sitio es https://www.abahanavillas.com/es/

    Args:
        url: URL completa de la página a consultar (debe ser de abahanavillas.com).

    Returns:
        Diccionario con 'titulo', 'contenido' (texto limpio) y 'url'.
    """
    if not _url_permitida(url):
        return {"error": f"Solo se permiten URLs https de {_DOMINIO_WEB}", "url": url}

    ahora = time.monotonic()
    cacheado = _CACHE_WEB.get(url)
    if cacheado and ahora - cacheado[0] < _CACHE_WEB_TTL:
        return cacheado[1]

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            estado, html = pool.submit(_playwright_en_hilo, url).result(timeout=45)
    except Exception as e:
        log.exception("consultar_web: error al obtener %s", url)
        return {"error": str(e), "url": url}

    # El sitio responde a las rutas inexistentes con una página de error que
    # se parece a la portada. Sin mirar el código, el agente daba por buena
    # esa respuesta y se quedaba con contenido que no había pedido.
    if estado >= 400:
        return {
            "error": (
                f"La página no existe (HTTP {estado}). Usa "
                f"buscar_pagina_web(...) para localizar la URL correcta."
            ),
            "url": url,
            "estado": estado,
        }

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "noscript", "iframe",
                     "img", "picture", "video", "audio", "svg", "canvas",
                     "select", "option", "input"]):
        tag.decompose()

    titulo = soup.title.string.strip() if soup.title else ""

    enlaces = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        texto_link = a.get_text(strip=True)
        if _DOMINIO_WEB in href or href.startswith("/"):
            if href.startswith("/"):
                href = f"https://www.{_DOMINIO_WEB}{href}"
            if texto_link:
                enlaces.append(f"{texto_link}: {href}")

    body = soup.find("body") or soup
    texto = body.get_text(separator="\n", strip=True)

    seen: set[str] = set()
    lineas = []
    for l in texto.splitlines():
        l = l.strip()
        if len(l) < 10 or l in seen:
            continue
        seen.add(l)
        lineas.append(l)

    contenido = "\n".join(lineas)

    enlaces_unicos = list(dict.fromkeys(enlaces))
    seccion_enlaces = "\n".join(enlaces_unicos[:50])

    resultado = {
        "titulo": titulo,
        "contenido": contenido[:9000],
        "enlaces": seccion_enlaces,
        "url": url,
    }

    # Las páginas corporativas cambian poco: cachearlas evita arrancar un
    # navegador entero cada vez que alguien pregunta por el aviso legal.
    _CACHE_WEB[url] = (ahora, resultado)
    if len(_CACHE_WEB) > _CACHE_WEB_MAX:
        del _CACHE_WEB[min(_CACHE_WEB, key=lambda k: _CACHE_WEB[k][0])]
    return resultado


_SITEMAP = f"https://www.{_DOMINIO_WEB}/sitemap.xml"
_PALABRAS_IGNORADAS = {
    "de", "del", "la", "el", "los", "las", "y", "en", "para", "por", "con",
    "que", "cual", "cuales", "es", "son", "un", "una", "sobre", "web",
    "pagina", "paginas",
}


def _normalizar(texto: str) -> str:
    from unicodedata import normalize
    sin_tildes = normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sin_tildes.lower()).strip()


@functools.lru_cache(maxsize=1)
def _sitemap_urls() -> tuple[str, ...]:
    """URLs en español publicadas en el sitemap del sitio.

    Se cachea: son más de mil y el sitemap cambia poco.
    """
    respuesta = requests.get(_SITEMAP, timeout=20)
    respuesta.raise_for_status()
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", respuesta.text)
    return tuple(u for u in urls if f"/{'es'}/" in u)


def buscar_pagina_web(consulta: str) -> dict[str, Any]:
    """Localiza páginas de la web de Abahana Villas por su tema.

    Úsala cuando no sepas la URL exacta, o cuando `consultar_web` responda que
    la página no existe. Devuelve URLs reales tomadas del sitemap del sitio,
    para no tener que adivinarlas. Después pásale la URL a `consultar_web`.

    Args:
        consulta: Tema a localizar ("preguntas frecuentes", "condiciones de
            alquiler", "aviso legal"...).

    Returns:
        Diccionario con 'paginas' (lista de {url, titulo}, máx. 8).
    """
    try:
        urls = _sitemap_urls()
    except Exception as e:
        log.exception("buscar_pagina_web: no se pudo leer el sitemap")
        return {"paginas": [], "error": str(e)}

    terminos = [
        t for t in _normalizar(consulta).split()
        if t and t not in _PALABRAS_IGNORADAS
    ]
    if not terminos:
        return {"paginas": []}

    puntuadas: list[tuple[int, str]] = []
    for url in urls:
        ruta = _normalizar(urlparse(url).path)
        aciertos = sum(1 for t in terminos if t in ruta)
        if aciertos:
            # A igualdad de aciertos, la ruta más corta suele ser la página
            # principal del tema y no una hoja del árbol.
            puntuadas.append((aciertos * 1000 - len(ruta), url))

    puntuadas.sort(reverse=True)
    return {
        "paginas": [
            {
                "url": url,
                "titulo": urlparse(url).path.rstrip("/").split("/")[-1]
                .replace("-", " "),
            }
            for _, url in puntuadas[:8]
        ]
    }


def buscar_internet(consulta: str) -> dict[str, Any]:
    """Busca información actual en internet via Google Search.

    OBLIGATORIO para preguntas sobre fiestas locales, eventos, clima,
    atracciones turísticas, horarios de mercados y datos de pueblos de la
    Costa Blanca (Calpe, Altea, Moraira, Dénia, Benidorm…).

    Args:
        consulta: Pregunta o tema a buscar, en español.

    Returns:
        Diccionario con 'respuesta' (texto) y 'fuentes' (lista de URLs).
    """
    try:
        response = _get_genai_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=consulta.strip(),
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                system_instruction=(
                    "Eres un asistente de búsqueda para Abahana Villas (Costa Blanca). "
                    "Responde SIEMPRE en español con información verificable y actual. "
                    "Prioriza fuentes oficiales: ayuntamientos, turismo, medios locales. "
                    "Incluye fechas concretas cuando existan."
                ),
            ),
        )
    except Exception as e:
        log.exception("buscar_internet: error en Google Search")
        return {"respuesta": "", "fuentes": [], "error": str(e)}

    texto = response.text or ""
    fuentes: list[dict[str, str]] = []
    if response.candidates:
        metadata = response.candidates[0].grounding_metadata
        if metadata and metadata.grounding_chunks:
            vistos: set[str] = set()
            for chunk in metadata.grounding_chunks:
                if chunk.web and chunk.web.uri and chunk.web.uri not in vistos:
                    vistos.add(chunk.web.uri)
                    fuentes.append({
                        "titulo": chunk.web.title or chunk.web.uri,
                        "url": chunk.web.uri,
                    })

    return {"respuesta": texto, "fuentes": fuentes}


def consultar_disponibilidad(
    fecha_desde: str,
    fecha_hasta: str | None = None,
    villa_nombre: str | None = None,
    ubicacion: str | None = None,
    zona: str | None = None,
    capacidad_min: int | None = None,
    habitaciones_min: int | None = None,
    camas_min: int | None = None,
    piscina: bool | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    """Busca villas libres en un periodo futuro y devuelve el total exacto.

    Esta es la única herramienta que debe usarse para afirmar que una villa
    está disponible o para contar villas libres. Comprueba solapamientos con
    reservas reales y rechaza fechas pasadas.

    Args:
        fecha_desde: Fecha de entrada solicitada (YYYY-MM-DD).
        fecha_hasta: Fecha de salida solicitada (YYYY-MM-DD, exclusiva). Si se
            omite, se comprueba una estancia de una noche desde fecha_desde.
        villa_nombre: Nombre o parte del nombre de una villa concreta.
        ubicacion: Pueblo cercano (Calpe, Altea, Moraira...).
        zona: Zona geográfica.
        capacidad_min: Número mínimo de huéspedes.
        habitaciones_min: Número mínimo de habitaciones (dormitorios).
        camas_min: Número mínimo de camas reales. No es lo mismo que
            habitaciones_min: una habitación puede tener varias camas.
        piscina: True para exigir piscina privada.
        limite: Máximo de villas a mostrar (el total siempre es exacto).

    Returns:
        Diccionario con available, total_disponibles, matches y periodo. Si las
        fechas no son válidas devuelve error y no consulta BigQuery.
    """
    hoy = _ahora_local().date()
    try:
        desde = _parse_iso_date(fecha_desde, "fecha_desde")
        hasta = (
            _parse_iso_date(fecha_hasta, "fecha_hasta")
            if fecha_hasta
            else desde + datetime.timedelta(days=1)
        )
    except ValueError as exc:
        return {
            "available": False,
            "total_disponibles": 0,
            "matches": [],
            "error": str(exc),
            "fecha_actual": hoy.isoformat(),
        }

    if desde < hoy:
        return {
            "available": False,
            "total_disponibles": 0,
            "matches": [],
            "error": (
                f"No se puede consultar disponibilidad para una fecha pasada. "
                f"La fecha actual en Europe/Madrid es {hoy.isoformat()}."
            ),
            "fecha_actual": hoy.isoformat(),
        }
    if hasta <= desde:
        return {
            "available": False,
            "total_disponibles": 0,
            "matches": [],
            "error": "fecha_hasta debe ser posterior a fecha_desde.",
            "fecha_actual": hoy.isoformat(),
        }

    # activo/visible los aplica el CTE de villas vigentes, antes de deduplicar.
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("fecha_desde", "DATE", desde),
        bigquery.ScalarQueryParameter("fecha_hasta", "DATE", hasta),
    ]
    if villa_nombre:
        conditions.append("LOWER(v.nombre) LIKE LOWER(@villa_nombre)")
        params.append(bigquery.ScalarQueryParameter(
            "villa_nombre", "STRING", f"%{villa_nombre.strip()}%"
        ))
    if ubicacion:
        conditions.append("LOWER(v.pueblo_cercano) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter(
            "ubicacion", "STRING", f"%{ubicacion.strip()}%"
        ))
    if zona:
        conditions.append("LOWER(v.zona) LIKE LOWER(@zona)")
        params.append(bigquery.ScalarQueryParameter(
            "zona", "STRING", f"%{zona.strip()}%"
        ))
    if capacidad_min is not None:
        conditions.append("v.capacidad_pax >= @capacidad_min")
        params.append(bigquery.ScalarQueryParameter(
            "capacidad_min", "INT64", capacidad_min
        ))
    if habitaciones_min is not None:
        conditions.append(f"v.{_columna_habitaciones()} >= @habitaciones_min")
        params.append(bigquery.ScalarQueryParameter(
            "habitaciones_min", "INT64", habitaciones_min
        ))
    if camas_min is not None:
        conditions.append("c.camas_totales >= @camas_min")
        params.append(bigquery.ScalarQueryParameter(
            "camas_min", "INT64", camas_min
        ))
    if piscina is not None:
        conditions.append(
            f"v.tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}"
        )

    limite = min(max(1, limite), 50)
    where = " AND ".join(conditions) if conditions else "TRUE"
    query = f"""
        WITH{_CTE_VILLAS_VIGENTES},{_CTE_CAMAS},
        disponibles AS (
            SELECT
                v.nombre,
                v.pueblo_cercano,
                v.zona,
                v.region,
                v.capacidad_pax,
                v.{_columna_habitaciones()} AS numero_habitaciones,
                v.numero_banos,
                v.tiene_piscina_privada,
                v.admite_animales
            FROM villa_dedup v
            LEFT JOIN camas c ON c.villa_id = v.villa_id
            WHERE {where}
              AND NOT EXISTS (
                  SELECT 1
                  FROM {TABLA_RESERVAS} r
                  WHERE r.villa_id = v.villa_id
                    AND COALESCE(r.es_activo, TRUE) = TRUE
                    AND UPPER(COALESCE(r.estado_reserva, '')) NOT IN (
                        'CA', 'CANCELACION', 'CANCELADA'
                    )
                    AND UPPER(COALESCE(r.estado_documento, '')) NOT IN (
                        'VO', 'ANULADA', 'ANULADO'
                    )
                    AND r.fecha_entrada < @fecha_hasta
                    AND r.fecha_salida > @fecha_desde
              )
        )
        SELECT *, COUNT(*) OVER() AS total_disponibles
        FROM disponibles
        ORDER BY nombre
        LIMIT {limite}
    """
    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as exc:
        log.exception("consultar_disponibilidad: error en BigQuery")
        return {
            "available": False,
            "total_disponibles": 0,
            "matches": [],
            "periodo": {
                "entrada": desde.isoformat(),
                "salida": hasta.isoformat(),
            },
            "error": str(exc),
        }

    matches = [_row_to_dict(row) for row in rows]
    total = int(matches[0].pop("total_disponibles")) if matches else 0
    for match in matches[1:]:
        match.pop("total_disponibles", None)
    return {
        "available": total > 0,
        "total_disponibles": total,
        "matches": matches,
        "periodo": {
            "entrada": desde.isoformat(),
            "salida": hasta.isoformat(),
            "zona_horaria": "Europe/Madrid",
        },
    }


def _rango_valido(desde: str, hasta: str | None, campo: str = "fecha_hasta"):
    """Valida el rango y lo acota: sin tope, una consulta de un año entero
    devuelve decenas de miles de filas de tarifa."""
    inicio = _parse_iso_date(desde, "fecha_desde")
    fin = _parse_iso_date(hasta, campo) if hasta else inicio
    if fin < inicio:
        raise ValueError(f"{campo} no puede ser anterior a fecha_desde.")
    if (fin - inicio).days + 1 > _MAX_DIAS_CONSULTA:
        raise ValueError(
            f"El rango no puede superar {_MAX_DIAS_CONSULTA} días; "
            "consulta por tramos más cortos."
        )
    return inicio, fin


def _redondear(valor):
    return round(valor, 2) if valor is not None else None


def consultar_precios(
    villa_nombre: str,
    fecha_desde: str,
    fecha_hasta: str | None = None,
) -> dict[str, Any]:
    """Precio de venta, precio de compra y margen de una villa, noche a noche.

    Usa esta herramienta para preguntas de gestión sobre tarifas, coste,
    margen o rentabilidad: "cuánto cuesta la noche", "cuánto ganamos",
    "qué margen deja esta villa en agosto", "cuál es el precio de larga
    estancia". Devuelve también los extras con precio propio (energía, aire
    acondicionado) y sus márgenes.

    Args:
        villa_nombre: Nombre o parte del nombre de la villa.
        fecha_desde: Primera noche (YYYY-MM-DD).
        fecha_hasta: Última noche (YYYY-MM-DD). Si se omite, solo esa noche.
            El rango no puede superar 92 días.

    Returns:
        Diccionario con 'noches' (venta, compra y margen por fecha),
        'resumen' del periodo, 'larga_estancia' y 'extras'.
    """
    try:
        desde, hasta = _rango_valido(fecha_desde, fecha_hasta)
    except ValueError as exc:
        return {"noches": [], "resumen": {}, "error": str(exc)}

    query = f"""
        WITH{_CTE_VILLAS_VIGENTES},
        ocupacion AS (
            SELECT o.id, o.fecha, o.estado
            FROM {TABLA_OCUPACION} o
            JOIN villa_dedup v ON v.villa_id = o.villa_id
            WHERE o.es_activo = TRUE
              AND o.fecha BETWEEN @desde AND @hasta
              AND LOWER(v.nombre) LIKE LOWER(@villa_nombre)
        )
        SELECT
            oc.fecha, oc.estado, t.nombre, t.tipo, t.es_venta,
            t.precio_final, t.descuento_pct, t.estancia_minima_noches
        FROM {TABLA_TARIFA_DIA} t
        JOIN ocupacion oc ON oc.id = t.ocupacion_id
        WHERE t.es_activo = TRUE
          AND t.precio_final IS NOT NULL
          -- Las filas sin es_venta duplican la tarifa de villa: contarlas
          -- inflaría los totales.
          AND t.es_venta IS NOT NULL
        ORDER BY oc.fecha
    """
    params = [
        bigquery.ScalarQueryParameter("desde", "DATE", desde),
        bigquery.ScalarQueryParameter("hasta", "DATE", hasta),
        bigquery.ScalarQueryParameter(
            "villa_nombre", "STRING", f"%{villa_nombre.strip()}%"
        ),
    ]
    try:
        rows = [_row_to_dict(r) for r in _bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP_DIARIO,
            ),
        ).result()]
    except Exception as e:
        log.exception("consultar_precios: error en BigQuery")
        return {"noches": [], "resumen": {}, "error": str(e)}

    return _componer_precios(rows, desde, hasta)


def _componer_precios(rows: list[dict], desde, hasta) -> dict[str, Any]:
    por_noche: dict[str, dict[str, Any]] = {}
    larga: dict[str, list] = {"venta": [], "descuento": [], "minimo": []}
    extras: dict[str, dict[str, list]] = {}

    for fila in rows:
        fecha = str(fila.get("fecha"))
        nombre = fila.get("nombre")
        es_venta = fila.get("es_venta")
        precio = fila.get("precio_final")
        if precio is None:
            continue

        if nombre == _CONCEPTO_VILLA:
            if fila.get("tipo") == "lt":
                if es_venta:
                    larga["venta"].append(precio)
                    larga["descuento"].append(fila.get("descuento_pct"))
                    larga["minimo"].append(fila.get("estancia_minima_noches"))
                continue
            noche = por_noche.setdefault(
                fecha, {"fecha": fecha, "estado": fila.get("estado")}
            )
            noche["precio_venta" if es_venta else "precio_compra"] = precio
        elif fila.get("tipo") != "lt":
            acumulado = extras.setdefault(nombre, {"venta": [], "compra": []})
            acumulado["venta" if es_venta else "compra"].append(precio)

    noches = []
    for fecha in sorted(por_noche):
        noche = por_noche[fecha]
        venta, compra = noche.get("precio_venta"), noche.get("precio_compra")
        if venta is not None and compra is not None:
            noche["margen"] = _redondear(venta - compra)
            noche["margen_pct"] = _redondear(
                (venta - compra) / venta * 100
            ) if venta else None
        noches.append(noche)

    con_ambos = [n for n in noches if n.get("margen") is not None]
    total_venta = sum(n["precio_venta"] for n in con_ambos)
    total_compra = sum(n["precio_compra"] for n in con_ambos)

    def _media(valores):
        limpios = [v for v in valores if v is not None]
        return _redondear(sum(limpios) / len(limpios)) if limpios else None

    return {
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "noches": noches,
        "resumen": {
            "noches_con_precio": len(con_ambos),
            "total_venta": _redondear(total_venta),
            "total_compra": _redondear(total_compra),
            "margen_total": _redondear(total_venta - total_compra),
            "margen_medio_pct": _media([n["margen_pct"] for n in con_ambos]),
            "precio_venta_medio": _media([n["precio_venta"] for n in con_ambos]),
        },
        "larga_estancia": {
            "precio_venta_medio": _media(larga["venta"]),
            "descuento_pct": _media(larga["descuento"]),
            "minimo_noches": max(larga["minimo"]) if larga["minimo"] else None,
        } if larga["venta"] else {},
        "extras": [
            {
                "concepto": nombre,
                "precio_venta_medio": _media(v["venta"]),
                "precio_compra_medio": _media(v["compra"]),
            }
            for nombre, v in sorted(extras.items())
        ],
    }


_ESTADOS_NO_OCUPADOS = {"Libre", "No Disponible"}


def calendario_villa(
    villa_nombre: str,
    fecha_desde: str,
    fecha_hasta: str | None = None,
) -> dict[str, Any]:
    """Calendario de ocupación de una villa, tramo a tramo.

    Usa esta herramienta para gestión interna: ver cómo está el calendario de
    una villa, qué días está libre, ocupada o bloqueada, y por qué canal entró
    cada reserva (directa, agencia, turoperador o uso del propietario).
    Agrupa los días consecutivos del mismo estado en tramos, que es como se
    lee un calendario.

    Para saber si una villa se puede vender en unas fechas concretas usa
    `consultar_disponibilidad`; esta herramienta es para ver el panorama.

    Args:
        villa_nombre: Nombre o parte del nombre de la villa.
        fecha_desde: Primer día (YYYY-MM-DD).
        fecha_hasta: Último día (YYYY-MM-DD). Máximo 92 días de rango.

    Returns:
        Diccionario con 'tramos' y 'resumen' (noches por estado, porcentaje de
        ocupación sobre los días comercializables y desglose por canal).
    """
    try:
        desde, hasta = _rango_valido(fecha_desde, fecha_hasta)
    except ValueError as exc:
        return {"tramos": [], "resumen": {}, "error": str(exc)}

    # No se leen es_checkin / es_checkout: vienen a TRUE en el ~95% de las
    # filas, libres u ocupadas, porque son reglas de llegada y salida
    # permitidas, no eventos de entrada o salida de huéspedes.
    query = f"""
        WITH{_CTE_VILLAS_VIGENTES}
        SELECT o.fecha, o.tipo_ocupacion, o.reserva_id, o.estancia_minima_noches
        FROM {TABLA_OCUPACION} o
        JOIN villa_dedup v ON v.villa_id = o.villa_id
        WHERE o.es_activo = TRUE
          AND o.fecha BETWEEN @desde AND @hasta
          AND LOWER(v.nombre) LIKE LOWER(@villa_nombre)
        ORDER BY o.fecha
    """
    params = [
        bigquery.ScalarQueryParameter("desde", "DATE", desde),
        bigquery.ScalarQueryParameter("hasta", "DATE", hasta),
        bigquery.ScalarQueryParameter(
            "villa_nombre", "STRING", f"%{villa_nombre.strip()}%"
        ),
    ]
    try:
        rows = [_row_to_dict(r) for r in _bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP_DIARIO,
            ),
        ).result()]
    except Exception as e:
        log.exception("calendario_villa: error en BigQuery")
        return {"tramos": [], "resumen": {}, "error": str(e)}

    return _componer_calendario(rows, desde, hasta)


def _componer_calendario(rows: list[dict], desde, hasta) -> dict[str, Any]:
    tramos: list[dict[str, Any]] = []
    por_estado: dict[str, int] = {}

    for fila in sorted(rows, key=lambda f: str(f.get("fecha"))):
        fecha = str(fila.get("fecha"))
        tipo = fila.get("tipo_ocupacion") or "Desconocido"
        reserva = fila.get("reserva_id")
        por_estado[tipo] = por_estado.get(tipo, 0) + 1

        ultimo = tramos[-1] if tramos else None
        if (
            ultimo
            and ultimo["tipo_ocupacion"] == tipo
            and ultimo.get("reserva_id") == reserva
        ):
            ultimo["hasta"] = fecha
            ultimo["noches"] += 1
        else:
            tramo = {
                "desde": fecha,
                "hasta": fecha,
                "tipo_ocupacion": tipo,
                "noches": 1,
            }
            if reserva:
                tramo["reserva_id"] = reserva
            if fila.get("estancia_minima_noches"):
                tramo["estancia_minima_noches"] = fila["estancia_minima_noches"]
            tramos.append(tramo)

    ocupadas = sum(
        n for estado, n in por_estado.items() if estado not in _ESTADOS_NO_OCUPADOS
    )
    libres = por_estado.get("Libre", 0)
    bloqueadas = por_estado.get("No Disponible", 0)
    comercializables = ocupadas + libres

    return {
        "periodo": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "tramos": tramos,
        "resumen": {
            "noches_ocupadas": ocupadas,
            "noches_libres": libres,
            "noches_bloqueadas": bloqueadas,
            # Sobre los días que se podían vender: las bloqueadas no cuentan.
            "ocupacion_pct": _redondear(ocupadas / comercializables * 100)
            if comercializables else None,
            "por_canal": {
                estado: n for estado, n in sorted(por_estado.items())
                if estado not in _ESTADOS_NO_OCUPADOS
            },
        },
    }


def consultar_reservas(
    villa_nombre: str | None = None,
    ubicacion: str | None = None,
    zona: str | None = None,
    piscina: bool | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    estado_reserva: str | None = None,
    estado_documento: str | None = None,
    excluir_canceladas: bool = True,
    limite: int = 20,
) -> dict[str, Any]:
    """Consulta reservas individuales con fechas, importes y datos del cliente.

    Usa para preguntas sobre reservas concretas: fechas de entrada/salida,
    importe, cliente y estado. No uses la ausencia de resultados para afirmar
    disponibilidad; para ello usa consultar_disponibilidad.
    Permite cruzar con ubicación y datos de la villa.

    Args:
        villa_nombre: Nombre o parte del nombre de la villa (ej. "NASSAU").
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira…).
        zona: Zona geográfica (Benissa Costa, Moraira…).
        piscina: True para filtrar villas con piscina privada.
        fecha_desde: Fecha de entrada desde (YYYY-MM-DD).
        fecha_hasta: Fecha de entrada hasta (YYYY-MM-DD).
        estado_reserva: 'RE'=reserva, 'PE'=perdida, 'CA'=cancelación,
                        'NS'=no show, 'PR'=prereserva, 'BO'=bloqueada.
        estado_documento: 'CO'=confirmado, 'DR'=borrador, 'CL'=cerrado, 'VO'=anulado.
        excluir_canceladas: Si True (defecto), excluye canceladas (CA) y anuladas (VO).
        limite: Máximo de resultados (defecto 20, máx. 50).

    Returns:
        Diccionario con 'reservas' (lista) y 'count'.
    """
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if villa_nombre:
        conditions.append("LOWER(r.villa_nombre) LIKE LOWER(@villa_nombre)")
        params.append(bigquery.ScalarQueryParameter("villa_nombre", "STRING", f"%{villa_nombre.strip()}%"))

    if ubicacion:
        conditions.append("LOWER(v.pueblo_cercano) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter("ubicacion", "STRING", f"%{ubicacion.strip()}%"))

    if zona:
        conditions.append("LOWER(v.zona) LIKE LOWER(@zona)")
        params.append(bigquery.ScalarQueryParameter("zona", "STRING", f"%{zona.strip()}%"))

    if piscina is not None:
        conditions.append(f"v.tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")

    if fecha_desde:
        conditions.append("r.fecha_entrada >= @fecha_desde")
        params.append(bigquery.ScalarQueryParameter("fecha_desde", "DATE", fecha_desde))

    if fecha_hasta:
        conditions.append("r.fecha_entrada <= @fecha_hasta")
        params.append(bigquery.ScalarQueryParameter("fecha_hasta", "DATE", fecha_hasta))

    if estado_reserva:
        conditions.append(
            _filtro_estado("r.estado_reserva", "estado_reserva",
                           estado_reserva, _ESTADOS_RESERVA, params)
        )

    if estado_documento:
        conditions.append(
            _filtro_estado("r.estado_documento", "estado_documento",
                           estado_documento, _ESTADOS_DOCUMENTO, params)
        )

    if excluir_canceladas:
        conditions.append(
            "UPPER(COALESCE(r.estado_reserva, '')) NOT IN "
            "('CA', 'CANCELACION', 'CANCELADA')"
        )
        conditions.append(
            "UPPER(COALESCE(r.estado_documento, '')) NOT IN "
            "('VO', 'ANULADA', 'ANULADO')"
        )

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limite = min(max(1, limite), 50)

    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            {_DEDUP_VILLA}
        )
        SELECT
            r.localizador,
            r.villa_nombre,
            r.cliente_nombre,
            r.fecha_entrada,
            r.fecha_salida,
            DATE_DIFF(r.fecha_salida, r.fecha_entrada, DAY) AS noches,
            r.adultos,
            r.ninos,
            r.num_mascotas,
            r.estado_reserva,
            r.estado_documento,
            r.importe_total,
            r.moneda_id,
            r.es_prereserva,
            r.es_alto_riesgo,
            r.es_cliente_nuevo,
            v.pueblo_cercano,
            v.zona,
            v.tiene_piscina_privada
        FROM {TABLA_RESERVAS} r
        LEFT JOIN villa_dedup v ON r.villa_id = v.villa_id
        {where}
        ORDER BY r.fecha_entrada DESC
        LIMIT {limite}
    """

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as e:
        log.exception("consultar_reservas: error en BigQuery")
        return {"reservas": [], "count": 0, "error": str(e)}

    reservas = [_row_to_dict(r) for r in rows]
    return {"reservas": reservas, "count": len(reservas)}


def resumen_reservas(
    agrupar_por: str = "villa",
    ubicacion: str | None = None,
    zona: str | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    excluir_canceladas: bool = True,
    limite: int = 20,
) -> dict[str, Any]:
    """Estadísticas agregadas de reservas: conteos, importes y noches medias.

    Usa para preguntas analíticas: cuántas reservas tiene cada villa,
    qué zona factura más, qué meses tienen más actividad, evolución anual.

    Args:
        agrupar_por: Dimensión: 'villa' (defecto), 'zona', 'mes', 'ano'.
        ubicacion: Filtra por pueblo cercano (Altea, Calpe, Moraira…).
        zona: Filtra por zona geográfica.
        fecha_desde: Fecha de entrada desde (YYYY-MM-DD).
        fecha_hasta: Fecha de entrada hasta (YYYY-MM-DD).
        excluir_canceladas: Si True (defecto), excluye canceladas y anuladas.
        limite: Máximo de filas (defecto 20, máx. 50).

    Returns:
        Diccionario con 'resumen' (lista con dimension, total_reservas,
        importe_total, importe_medio, noches_medias) y 'count'.
    """
    _GROUP_OPTIONS: dict[str, tuple[str, str]] = {
        "villa": ("r.villa_nombre",                         "r.villa_nombre"),
        "zona":  ("v.zona",                                 "v.zona"),
        "mes":   ("FORMAT_DATE('%Y-%m', r.fecha_entrada)",  "FORMAT_DATE('%Y-%m', r.fecha_entrada)"),
        "ano":   ("EXTRACT(YEAR FROM r.fecha_entrada)",     "EXTRACT(YEAR FROM r.fecha_entrada)"),
    }
    group_key = agrupar_por.lower() if agrupar_por.lower() in _GROUP_OPTIONS else "villa"
    select_expr, group_expr = _GROUP_OPTIONS[group_key]

    conditions: list[str] = ["r.fecha_entrada IS NOT NULL"]
    params: list[bigquery.ScalarQueryParameter] = []

    if ubicacion:
        conditions.append("LOWER(v.pueblo_cercano) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter("ubicacion", "STRING", f"%{ubicacion.strip()}%"))

    if zona:
        conditions.append("LOWER(v.zona) LIKE LOWER(@zona)")
        params.append(bigquery.ScalarQueryParameter("zona", "STRING", f"%{zona.strip()}%"))

    if fecha_desde:
        conditions.append("r.fecha_entrada >= @fecha_desde")
        params.append(bigquery.ScalarQueryParameter("fecha_desde", "DATE", fecha_desde))

    if fecha_hasta:
        conditions.append("r.fecha_entrada <= @fecha_hasta")
        params.append(bigquery.ScalarQueryParameter("fecha_hasta", "DATE", fecha_hasta))

    if excluir_canceladas:
        conditions.append(
            "UPPER(COALESCE(r.estado_reserva, '')) NOT IN "
            "('CA', 'CANCELACION', 'CANCELADA')"
        )
        conditions.append(
            "UPPER(COALESCE(r.estado_documento, '')) NOT IN "
            "('VO', 'ANULADA', 'ANULADO')"
        )

    where = f"WHERE {' AND '.join(conditions)}"
    limite = min(max(1, limite), 50)

    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            {_DEDUP_VILLA}
        )
        SELECT
            {select_expr} AS dimension,
            COUNT(*) AS total_reservas,
            ROUND(SUM(r.importe_total), 2) AS importe_total,
            ROUND(AVG(r.importe_total), 2) AS importe_medio,
            ROUND(AVG(DATE_DIFF(r.fecha_salida, r.fecha_entrada, DAY)), 1) AS noches_medias,
            MIN(r.fecha_entrada) AS primera_entrada,
            MAX(r.fecha_entrada) AS ultima_entrada
        FROM {TABLA_RESERVAS} r
        LEFT JOIN villa_dedup v ON r.villa_id = v.villa_id
        {where}
        GROUP BY {group_expr}
        ORDER BY total_reservas DESC
        LIMIT {limite}
    """

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as e:
        log.exception("resumen_reservas: error en BigQuery")
        return {"resumen": [], "count": 0, "error": str(e)}

    resumen = [_row_to_dict(r) for r in rows]
    return {"resumen": resumen, "count": len(resumen)}


def consultar_feedback_negativo() -> dict[str, Any]:
    """Consulta respuestas recientemente mal valoradas o parciales por los usuarios.

    Usa esta herramienta cuando quieras evitar repetir errores frecuentes
    o cuando el usuario indique que una respuesta anterior no fue útil.
    Devuelve ejemplos con motivos y comentarios cuando estén disponibles.

    Returns:
        Diccionario con 'examples' (lista) y 'count'.
    """
    from conversation_store import get_conversation_store

    examples = get_conversation_store().get_recent_problematic_examples(limit=8)
    return {"examples": examples, "count": len(examples)}


def listar_tablas_disponibles() -> dict[str, Any]:
    """Lista las tablas disponibles en el dataset silver_clean, con descripción si existe.

    Usa esta herramienta para explorar qué tablas hay cuando ninguna otra
    herramienta cubra lo que se pregunta, o si sospechas que una tabla fija
    (villa, reserva...) ha cambiado de nombre.

    Returns:
        Diccionario con 'tables' (lista de {table_name, descripcion}).
    """
    query = f"""
        SELECT t.table_name, o.option_value AS descripcion
        FROM `{PROJECT_ID}.{DATASET}.INFORMATION_SCHEMA.TABLES` t
        LEFT JOIN `{PROJECT_ID}.{DATASET}.INFORMATION_SCHEMA.TABLE_OPTIONS` o
            ON t.table_name = o.table_name AND o.option_name = 'description'
        ORDER BY t.table_name
    """
    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_BILLING_CAP),
        ).result())
    except Exception as e:
        log.exception("listar_tablas_disponibles: error en BigQuery")
        return {"tables": [], "error": str(e)}
    return {"tables": [_row_to_dict(r) for r in rows]}


def describir_tabla(nombre_tabla: str) -> dict[str, Any]:
    """Devuelve las columnas y tipos de una tabla del dataset silver_clean.

    Usa esto antes de `ejecutar_sql` para confirmar nombres exactos de
    columnas, sobre todo si una tabla fija ha podido cambiar de estructura.

    Args:
        nombre_tabla: nombre exacto de la tabla (ver listar_tablas_disponibles).

    Returns:
        Diccionario con 'columns' (lista de {column_name, data_type}).
    """
    query = f"""
        SELECT column_name, data_type
        FROM `{PROJECT_ID}.{DATASET}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = @nombre_tabla
        ORDER BY ordinal_position
    """
    params = [bigquery.ScalarQueryParameter("nombre_tabla", "STRING", nombre_tabla)]
    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=_BILLING_CAP),
        ).result())
    except Exception as e:
        log.exception("describir_tabla: error en BigQuery")
        return {"columns": [], "error": str(e)}
    return {"columns": [_row_to_dict(r) for r in rows]}


# Referencias de tabla: sirve tanto `proj.dataset.tabla` entre acentos graves
# como dataset.tabla sin ellos. Un identificador suelto es un CTE o un alias.
_REF_TABLA = re.compile(
    r"\b(?:FROM|JOIN)\s+(`[^`]+`|[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)+)",
    re.IGNORECASE,
)


def _proyecto_y_dataset(referencia: str) -> tuple[str | None, str]:
    partes = [t.strip("` ") for t in referencia.strip("`").split(".")]
    if len(partes) == 2:                                   # dataset.tabla
        return None, partes[0]
    if len(partes) == 3 and partes[1].upper() == "INFORMATION_SCHEMA":
        return None, partes[0]                             # dataset.INFO_SCHEMA.x
    return partes[0], partes[1]                            # proyecto.dataset[...]


def _motivo_rechazo_sql(query: str) -> str | None:
    """Devuelve por qué se rechaza la consulta, o None si es aceptable.

    El service account tiene bigquery.dataViewer a nivel de PROYECTO, así que
    sin esta comprobación `ejecutar_sql` podría leer cualquier dataset —
    incluido el de conversaciones almacenadas de todos los usuarios.
    """
    limpia = query.strip()
    if not limpia:
        return "La consulta está vacía."

    if ";" in limpia.rstrip().rstrip(";"):
        return (
            "Solo se admite una sentencia por consulta (no uses ';' para "
            "encadenar varias)."
        )

    if not limpia.upper().startswith(("SELECT", "WITH")):
        return "Solo se permiten consultas SELECT o WITH."

    for referencia in _REF_TABLA.findall(limpia):
        proyecto, dataset = _proyecto_y_dataset(referencia)
        if dataset.lower() != DATASET.lower():
            return (
                f"Solo se puede consultar el dataset {DATASET}; "
                f"'{referencia}' apunta a otro sitio."
            )
        if proyecto is not None and proyecto.lower() != PROJECT_ID.lower():
            return (
                f"Solo se puede consultar el proyecto {PROJECT_ID}; "
                f"'{referencia}' apunta a otro."
            )
    return None


def ejecutar_sql(query: str) -> dict[str, Any]:
    """Ejecuta una consulta SQL de solo lectura (SELECT/WITH) sobre silver_clean.

    Usa esta herramienta SOLO cuando ninguna otra tool cubra lo que necesitas,
    o cuando una tabla fija (villa, reserva...) haya cambiado de nombre o
    columnas. Antes de usarla, llama a `listar_tablas_disponibles()` y
    `describir_tabla(...)` para confirmar los nombres exactos.

    IMPORTANTE — trampas conocidas del dataset:
    - `stg_etendo_Villa` trae filas duplicadas por villa_id. Si la consultas,
      añade `QUALIFY ROW_NUMBER() OVER (PARTITION BY villa_id ORDER BY
      fecha_actualizacion DESC) = 1` para quedarte con una fila por villa.
    - Para relacionar `stg_etendo_Planta`/`stg_etendo_Banio` con una villa usa
      `villa_id` (no `identificador`/`villa_nombre`, que no casan).
    - Solo se permiten SELECT/WITH (nada de INSERT/UPDATE/DELETE/DDL) y una
      sola sentencia por llamada.
    - Solo se puede leer el dataset silver_clean; cualquier otro se rechaza.
    - El resultado se limita a 50 filas.

    Args:
        query: sentencia SQL SELECT/WITH completa.

    Returns:
        Diccionario con 'rows' (máx. 50), 'count' y, si aplica, 'error'.
    """
    motivo = _motivo_rechazo_sql(query)
    if motivo:
        return {"rows": [], "count": 0, "error": motivo}

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_BILLING_CAP),
        ).result(max_results=50))
    except Exception as e:
        log.exception("ejecutar_sql: error en BigQuery")
        return {"rows": [], "count": 0, "error": str(e)}

    data = [_row_to_dict(r) for r in rows]
    return {"rows": data, "count": len(data)}


# ---------------------------------------------------------------------------
# Instrucciones por rol
# ---------------------------------------------------------------------------

_INSTRUCCION_BASE = """
Eres el asistente virtual de Abahana Villas, empresa de alquiler de villas vacacionales
en la Costa Blanca (España). Ayudas con villas Y con información turística local.

## Reglas generales
- Responde SIEMPRE en español.
- Para ubicación usa pueblo_cercano (Altea, Calpe, Moraira…) o zona.
- Habitaciones y camas NO son lo mismo. "4 habitaciones" es `habitaciones_min=4`;
  "duerme a 8 en camas" es `camas_min=8`. Una habitación puede tener varias camas.
- Muestra el rating_medio cuando uses buscar_por_valoracion.
- Puedes combinar varios filtros en una sola llamada.
- Nunca inventes datos. Si no hay resultados en BigQuery, sugiere alternativas.
- Muestra los datos de forma clara: nombre, ubicación, capacidad, amenidades.
- No tenemos información de precios por noche en el sistema actual.
- NUNCA digas que solo puedes ayudar con villas si la pregunta es sobre turismo,
  fiestas, eventos o clima: usa `buscar_internet` primero.

## Fecha, hora y disponibilidad
- La zona horaria del negocio y del agente es `Europe/Madrid`.
- Para cualquier referencia relativa ("hoy", "mañana", "este fin de semana",
  "este mes", etc.), llama primero a `obtener_fecha_hora_actual()`. No inventes
  la fecha ni pidas al usuario un rango si su expresión relativa es suficiente.
- Para saber si una villa está libre o contar villas libres, llama SIEMPRE a
  `consultar_disponibilidad(...)`. La ausencia de filas en `consultar_reservas`
  no demuestra disponibilidad.
- Nunca afirmes disponibilidad si la herramienta devuelve `error`.
- No se puede reservar ni consultar disponibilidad para fechas pasadas. Explica
  el rechazo indicando la fecha actual devuelta por la herramienta.
- En un rango, `fecha_desde` es la entrada y `fecha_hasta` es la salida
  (la fecha de salida no se ocupa). Para consultar solo un día, omite
  `fecha_hasta`; la herramienta comprobará una noche.

## Web corporativa
- URL base: https://www.abahanavillas.com/es/
- Usa `consultar_web(url)` cuando el usuario pregunte por información de la
  empresa: condiciones, recargos, política de privacidad, aviso legal,
  contacto, destinos, etc.
- Páginas clave (usa estas directamente, no las adivines):
  - Preguntas frecuentes del usuario — condiciones de reserva, pagos, fianzas,
    mascotas y demás cargos accesorios:
    https://www.abahanavillas.com/es/ayuda-y-preguntas-frecuentes/preguntas-frecuentes-del-usuario
  - Preguntas frecuentes del propietario:
    https://www.abahanavillas.com/es/ayuda-y-preguntas-frecuentes/preguntas-frecuentes-del-propietario
  - Condiciones generales de uso y alquiler:
    https://www.abahanavillas.com/es/condiciones-generales-uso-y-alquiler
  - Condiciones del bono descuento:
    https://www.abahanavillas.com/es/condiciones-bono-descuento
  - Aviso legal: https://www.abahanavillas.com/es/aviso-legal
  - Política de privacidad: https://www.abahanavillas.com/es/politica-de-privacidad
  - Política de cookies: https://www.abahanavillas.com/es/politica-de-cookies
  - Contacto: https://www.abahanavillas.com/es/contacto
- QUÉ villas admiten algo (mascotas, piscina...) es una búsqueda en el
  catálogo: `buscar_propiedades(admite_animales=True)`. La web no lista villas.
- CUÁNTO cuesta un extra o bajo qué condiciones (mascotas, fianza, limpieza,
  cambios de reserva) sí está en la web, y en las preguntas frecuentes del
  usuario antes que en ningún otro sitio: ahí suele estar la cifra concreta,
  mientras que las condiciones generales solo remiten a la ficha de la villa.
- Si te preguntan si se admite algo y hay un importe asociado, responde a las
  dos cosas: cuántas villas lo admiten y cuánto cuesta.
- NO inventes URLs. Si `consultar_web` responde que la página no existe, o si
  el tema no está en la lista de arriba, llama a `buscar_pagina_web(consulta)`
  para localizar la URL real en el sitemap y consulta después esa.

## Búsqueda en internet — OBLIGATORIO para turismo local
- Si preguntan por fiestas, eventos, clima, atracciones, horarios o datos de pueblos,
  DEBES llamar a `buscar_internet(consulta=...)` ANTES de responder.
- No digas que no tienes esa información sin haber buscado en internet.
- Ejemplo: "¿cuándo son las fiestas de Calpe?" →
  buscar_internet(consulta="fechas fiestas patronales Calpe 2026")
- Para preguntas mixtas (ej. "villas en Calpe y cuándo son las fiestas"), combina
  herramientas de BigQuery con buscar_internet.
- Cita las fuentes cuando uses información obtenida de internet.

## Retroalimentación de usuarios
- El usuario puede valorar tus respuestas como **Útil**, **Parcial** o **No resolvió**.
- En valoraciones parciales o negativas puede indicar motivos (datos faltantes, mala interpretación,
  resultados poco relevantes, respuesta genérica) y un comentario libre.
- Si recibes contexto de retroalimentación en el mensaje, ajústalo en consecuencia.
- Usa `consultar_feedback_negativo()` si necesitas ver qué respuestas han fallado o quedado
  incompletas recientemente en otras conversaciones para no repetir los mismos errores.
""".strip()

INSTRUCTION_CLIENTE = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
- `obtener_fecha_hora_actual()`: fecha y hora actual en Europe/Madrid; obligatoria
  para expresiones relativas como hoy o mañana.
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad,
  habitaciones (`habitaciones_min`), camas reales (`camas_min`),
  baños, metros habitables, piscina privada, mascotas, internet, aire acondicionado,
  lavadora, lavavajillas.
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo (rating_exterior, rating_interior, rating_vistas en escala 1-6).
- `consultar_disponibilidad(...)`: disponibilidad real y total exacto de villas
  libres en un periodo; rechaza fechas pasadas.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `buscar_internet(consulta)`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…). OBLIGATORIO para esas preguntas.
- `consultar_feedback_negativo()`: respuestas parciales o no resueltas recientemente, con motivos.
""".strip()

INSTRUCTION_INTERNO = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
- `obtener_fecha_hora_actual()`: fecha y hora actual en Europe/Madrid; obligatoria
  para expresiones relativas como hoy o mañana.
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad,
  habitaciones (`habitaciones_min`), camas reales (`camas_min`),
  baños, metros habitables, piscina privada, mascotas, internet, aire acondicionado,
  lavadora, lavavajillas.
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo.
- `consultar_disponibilidad(...)`: única fuente para afirmar disponibilidad o
  contar villas libres; comprueba solapamientos y rechaza fechas pasadas.
- `obtener_detalle_propiedad(nombre)`: ficha completa con dirección, coordenadas,
  desglose de camas, metros habitables, ratings por categoría, amenidades
  (internet, aire acondicionado, lavadora, lavavajillas) y desglose real de los
  baños (cuántos tienen bañera, ducha, jacuzzi, bidé o son en-suite). Úsala
  cuando el usuario pregunte por una villa concreta o pida más detalles.
- `consultar_reservas(...)`: reservas individuales con fechas de entrada/salida,
  importe, cliente y estado. Filtra por villa, ubicación, zona, piscina, rango de
  fechas, estado_reserva ('RE'=reserva, 'PE'=perdida, 'CA'=cancelación,
  'NS'=no show, 'PR'=prereserva, 'BO'=bloqueada) y estado_documento
  ('CO'=confirmado, 'DR'=borrador, 'CL'=cerrado, 'VO'=anulado). No deduzcas
  disponibilidad de esta herramienta; usa `consultar_disponibilidad`.
- `resumen_reservas(...)`: estadísticas agregadas de reservas (count, importe total/
  medio, noches medias). Agrupa por 'villa', 'zona', 'mes' o 'ano'. Usa para
  preguntas analíticas: qué villa tiene más reservas, qué zona factura más, etc.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `buscar_internet(consulta)`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…). OBLIGATORIO para esas preguntas.
- `consultar_feedback_negativo()`: respuestas parciales o no resueltas recientemente, con motivos.
- `listar_tablas_disponibles()` / `describir_tabla(nombre_tabla)` / `ejecutar_sql(query)`:
  úsalas SOLO si ninguna herramienta anterior cubre la pregunta, o si una tabla fija
  parece haber cambiado de nombre/columnas. Explora primero con las dos primeras antes
  de escribir la query. Ten en cuenta las trampas documentadas en `ejecutar_sql`
  (duplicados en stg_etendo_Villa, join por villa_id en Planta/Banio).

## Contexto de uso interno
Eres la versión para agentes de ventas y equipo interno. Puedes mostrar la dirección
completa, coordenadas, datos de reservas e importes.
""".strip()

INSTRUCTION_ADMIN = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
- `obtener_fecha_hora_actual()`: fecha y hora actual en Europe/Madrid; obligatoria
  para expresiones relativas como hoy o mañana.
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad,
  habitaciones (`habitaciones_min`), camas reales (`camas_min`),
  baños, metros habitables, piscina privada, mascotas, internet, aire acondicionado,
  lavadora, lavavajillas.
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo (rating_exterior, rating_interior, rating_vistas en escala 1-6).
- `consultar_disponibilidad(...)`: única fuente para afirmar disponibilidad o
  contar villas libres; comprueba solapamientos y rechaza fechas pasadas.
- `obtener_detalle_propiedad(nombre)`: ficha completa con dirección, coordenadas,
  metros, ratings, propietario, amenidades (internet, aire acondicionado,
  lavadora, lavavajillas) y desglose real de los baños (bañera, ducha, jacuzzi,
  bidé, en-suite).
- `consultar_reservas(...)`: reservas individuales con fechas de entrada/salida,
  importe y estado. Filtra por villa, ubicación, zona, piscina, rango de fechas,
  estado_reserva ('RE'=reserva, 'PE'=perdida, 'CA'=cancelación, 'NS'=no show,
  'PR'=prereserva, 'BO'=bloqueada) y estado_documento ('CO'=confirmado, 'DR'=borrador,
  'CL'=cerrado, 'VO'=anulado). No la uses para afirmar disponibilidad.
- `resumen_reservas(...)`: estadísticas agregadas (count, importe total/medio, noches
  medias). Agrupa por 'villa', 'zona', 'mes' o 'ano'. Para preguntas analíticas:
  qué villa tiene más reservas, qué zona factura más, evolución mensual, etc.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `buscar_internet(consulta)`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…). OBLIGATORIO para esas preguntas.
- `consultar_feedback_negativo()`: respuestas parciales o no resueltas recientemente, con motivos.
- `listar_tablas_disponibles()` / `describir_tabla(nombre_tabla)` / `ejecutar_sql(query)`:
  úsalas SOLO si ninguna herramienta anterior cubre la pregunta, o si una tabla fija
  parece haber cambiado de nombre/columnas. Explora primero con las dos primeras antes
  de escribir la query. Ten en cuenta las trampas documentadas en `ejecutar_sql`
  (duplicados en stg_etendo_Villa, join por villa_id en Planta/Banio).

## Contexto de uso
Eres la versión de administración. Tienes acceso completo a todos los datos disponibles.
""".strip()


# ---------------------------------------------------------------------------
# Agentes por rol
# ---------------------------------------------------------------------------

agent_cliente = Agent(
    name="abahana_villas_agent_cliente",
    model="gemini-2.5-flash",
    description=(
        "Asistente de Abahana Villas: villas vacacionales, web corporativa "
        "e información turística local (fiestas, eventos, clima)."
    ),
    instruction=INSTRUCTION_CLIENTE,
    # Sin consultar_feedback_negativo: devuelve preguntas y respuestas de otros
    # usuarios, que no deben llegar a un cliente.
    tools=[
        obtener_fecha_hora_actual,
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        consultar_disponibilidad,
        consultar_web,
        buscar_pagina_web,
        buscar_internet,
    ],
)

agent_interno = Agent(
    name="abahana_villas_agent_interno",
    model="gemini-2.5-flash",
    description=(
        "Asistente interno de Abahana Villas: villas, fichas completas, reservas, "
        "web corporativa e información turística local (fiestas, eventos, clima)."
    ),
    instruction=INSTRUCTION_INTERNO,
    tools=[
        obtener_fecha_hora_actual,
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
        consultar_disponibilidad,
        consultar_reservas,
        resumen_reservas,
        # Solo interno y admin: consultar_precios expone precio de compra y
        # margen, y el calendario, el uso que hace el propietario de su villa.
        consultar_precios,
        calendario_villa,
        consultar_web,
        buscar_pagina_web,
        buscar_internet,
        consultar_feedback_negativo,
        listar_tablas_disponibles,
        describir_tabla,
        ejecutar_sql,
    ],
)

agent_admin = Agent(
    name="abahana_villas_agent_admin",
    model="gemini-2.5-flash",
    description=(
        "Asistente de administración de Abahana Villas: villas, fichas completas, "
        "reservas, web corporativa e información turística local (fiestas, eventos, clima)."
    ),
    instruction=INSTRUCTION_ADMIN,
    tools=[
        obtener_fecha_hora_actual,
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
        consultar_disponibilidad,
        consultar_reservas,
        resumen_reservas,
        # Solo interno y admin: consultar_precios expone precio de compra y
        # margen, y el calendario, el uso que hace el propietario de su villa.
        consultar_precios,
        calendario_villa,
        consultar_web,
        buscar_pagina_web,
        buscar_internet,
        consultar_feedback_negativo,
        listar_tablas_disponibles,
        describir_tabla,
        ejecutar_sql,
    ],
)

AGENTS: dict[str, Agent] = {
    "cliente": agent_cliente,
    "interno": agent_interno,
    "admin": agent_admin,
}

# Compatibilidad con `adk web` (espera root_agent en agente_villas/__init__.py)
root_agent = agent_interno
