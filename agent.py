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
import difflib
import functools
import logging
import os
import re
import time
import types
import typing
import unicodedata
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google import genai
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.cloud import bigquery
from google.genai import types as genai_types

log = logging.getLogger("agente-villas")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
DATASET = "silver_clean"
TABLA_VILLA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Villa`"
TABLA_RESERVAS = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Reserva`"
TABLA_TERCERO = f"`{PROJECT_ID}.{DATASET}.stg_etendo_Tercero`"
TABLA_PLAN_PAGOS = f"`{PROJECT_ID}.{DATASET}.stg_etendo_PlanFacturacion`"
TABLA_LINEA_RESERVA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_LineaReserva`"
TABLA_CONDICION_PAGO = f"`{PROJECT_ID}.{DATASET}.stg_etendo_CondicionesDePago`"
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
_DIAS_PRECIO_POR_DEFECTO = 30
# Ocupacion (1 M de filas) y TarifaDia (1,4 M) son las tablas de grano diario.
# Ninguna está particionada por fecha ni agrupada por villa, así que cualquier
# consulta las escanea enteras: ~60 MB y ~190 MB respectivamente, por encima
# del tope general. Se les da un tope propio en vez de subirlo para todas las
# herramientas. Particionar Ocupacion por fecha y agrupar TarifaDia por
# ocupacion_id en Dataform dejaría esto en una fracción.
_BILLING_CAP_DIARIO = 500 * 1024 * 1024

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

# Las valoraciones vienen de la ficha técnica, que va dentro de
# stg_etendo_Villa. La media es sobre baños, cocina, interior y exterior: las
# cuatro que el docstring de buscar_por_valoracion ha descrito siempre.
_RATING_MEDIO = """
            ROUND((COALESCE(v.score_rating_banos, 0) + COALESCE(v.score_rating_cocina, 0) +
                   COALESCE(v.score_rating_interior, 0) + COALESCE(v.score_rating_exterior, 0)) /
                  NULLIF((CASE WHEN v.score_rating_banos IS NOT NULL THEN 1 ELSE 0 END +
                          CASE WHEN v.score_rating_cocina IS NOT NULL THEN 1 ELSE 0 END +
                          CASE WHEN v.score_rating_interior IS NOT NULL THEN 1 ELSE 0 END +
                          CASE WHEN v.score_rating_exterior IS NOT NULL THEN 1 ELSE 0 END), 0),
                  2) AS rating_medio"""

# Proyección corta a propósito: todo lo que devuelve una herramienta entra en
# el contexto del modelo. La ficha completa de una villa concreta se pide con
# obtener_detalle_propiedad.
# Toda lista de villas trae las dos distancias: sin ellas, tras unas
# alternativas el modelo llegó a inventar una ("WATERFRONT, a 400 m", y estaba
# a 2,3 km). El mar y la playa de arena no son lo mismo (LOVELY: 600 m y 2 km).
# El 0 es "sin registrar", no "al lado".
_COLUMNAS_DISTANCIA = """
            NULLIF(v.distancia_mar_m, 0) AS distancia_mar_m,
            NULLIF(v.distancia_playa_arena_m, 0) AS distancia_playa_arena_m"""

_COLUMNAS_RESUMEN = f"""
            v.nombre, v.tipovilla_nombre_comercial,
            v.capacidad_pax, v.numero_banos, v.m2_habitables,
            COALESCE(c.camas_totales, 0) AS camas_totales,
            v.tiene_piscina_privada, v.admite_animales,
            v.tiene_internet, v.tiene_aire_acondicionado,
            v.tiene_lavadora, v.tiene_lavavajillas,
            v.tiene_vista_mar,{_COLUMNAS_DISTANCIA},
            v.zona_tranquila,
            v.pueblo_cercano, v.zona,{_RATING_MEDIO},
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


# Equipamiento de la ficha por el que se puede filtrar: parámetro -> expresión.
# La barbacoa, como el jacuzzi o el billar, va por `caracteristicas`.
_EQUIPAMIENTO = {
    "internet": "v.tiene_internet",
    "aire_acondicionado": "v.tiene_aire_acondicionado",
    "lavadora": "v.tiene_lavadora",
    "lavavajillas": "v.tiene_lavavajillas",
    "vista_mar": "v.tiene_vista_mar",
    "zona_tranquila": "v.zona_tranquila",
    "gimnasio": "v.tiene_gimnasio",
    "accesible": "v.apto_movilidad_reducida",
    "jacuzzi": "v.tiene_jacuzzi",
    "billar": "v.tiene_billar",
    "futbolin": "v.tiene_futbolin",
    "garaje": "v.tiene_garaje",
    "parking": ("(v.tiene_garaje OR v.tiene_parking_calle "
                "OR v.tiene_parking_cubierto OR v.tiene_parking_descubierto)"),
    "terraza": "(v.tiene_terraza_cubierta OR v.tiene_terraza_descubierta)",
}

# "Calpe o Moraira" son dos pueblos: el agente pasaba "Calpe | Moraira" y se
# buscaba ese texto tal cual, con 0 resultados y un falso "no hay villas".
_SEPARADOR_LUGARES = re.compile(r"\s*(?:\||,|/|;|\s+o\s+|\s+y\s+)\s*", re.IGNORECASE)


def _condicion_lugar(columna: str, nombre: str, valor: str, params: list) -> str:
    """Filtro por pueblo o zona; con varios, vale cualquiera de ellos."""
    lugares = [l for l in _SEPARADOR_LUGARES.split(valor.strip()) if l]
    if len(lugares) <= 1:
        params.append(bigquery.ScalarQueryParameter(nombre, "STRING", f"%{valor.strip()}%"))
        return f"LOWER({columna}) LIKE LOWER(@{nombre})"
    partes = []
    for i, lugar in enumerate(lugares[:6]):
        params.append(bigquery.ScalarQueryParameter(f"{nombre}_{i}", "STRING", f"%{lugar}%"))
        partes.append(f"LOWER({columna}) LIKE LOWER(@{nombre}_{i})")
    return "(" + " OR ".join(partes) + ")"


def _condiciones_equipamiento(**filtros: bool | None) -> list[str]:
    """Exigirlo compara contra TRUE; no exigirlo trata el dato ausente como
    ausencia, para no descartar las villas sin ficha."""
    condiciones = []
    for nombre, valor in filtros.items():
        if valor is None:
            continue
        expresion = _EQUIPAMIENTO[nombre]
        condiciones.append(
            f"{expresion} = TRUE" if valor
            else f"COALESCE({expresion}, FALSE) = FALSE"
        )
    return condiciones


# Cualquier dato de la ficha sirve como filtro con el parámetro `ficha`. Los
# de acceso (códigos de alarma, wifi) y los comerciales (fianza, comisión) no:
# no son criterios de búsqueda, y la versión cliente podría deducir su valor
# filtrando por ellos.
_SECCIONES_NO_FILTRABLES = {"acceso_seguridad", "comercial"}
_SINONIMOS_FICHA = {
    "ping_pong": "pingpong", "mesa_de_ping_pong": "pingpong",
    "tenis": "pista_tenis", "pista_de_tenis": "pista_tenis",
    "padel": "pista_padel", "pista_de_padel": "pista_padel",
    "wifi": "internet", "aire": "aire_acondicionado",
    "bbq": "barbacoa", "barbacoa_de_obra": "barbacoa_obra",
}
_CONDICION_FICHA = re.compile(
    r"^\s*(?P<neg>(?:sin|no)\s+|!)?(?P<campo>[^<>=!]+?)\s*"
    r"(?:(?P<op>>=|<=|!=|=|>|<)\s*(?P<valor>.+?))?\s*$",
    re.IGNORECASE,
)


@functools.lru_cache(maxsize=1)
def _tipos_columnas_villa() -> dict[str, str]:
    """Tipo de cada columna de la villa, leído del esquema (una vez por proceso)."""
    tabla = _bq.get_table(TABLA_VILLA.strip("`"))
    return {campo.name: campo.field_type for campo in tabla.schema}


def _columnas_filtrables() -> dict[str, str]:
    tipos = _tipos_columnas_villa()
    nombres = set(_FICHA_BASICA) | {
        columna
        for seccion, columnas in _SECCIONES_FICHA.items()
        if seccion not in _SECCIONES_NO_FILTRABLES
        for columna in columnas
    }
    return {c: tipos[c] for c in nombres if c in tipos}


def _clave_ficha(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[\s\-]+", "_", sin_tildes.strip().lower())


def _resolver_campo_ficha(texto: str, columnas: dict[str, str]) -> str | None:
    clave = _clave_ficha(texto)
    clave = _SINONIMOS_FICHA.get(clave, clave)
    for candidata in (clave, f"tiene_{clave}", f"num_{clave}"):
        if candidata in columnas:
            return candidata
    return None


def _condiciones_ficha(condiciones: list[str] | None,
                       params: list) -> list[str] | str:
    """SQL para cada condición sobre la ficha, o un error que el modelo entienda.

    Solo admite columnas de la ficha (lista blanca del esquema real) y los
    valores van siempre como parámetros: nada del texto llega al SQL tal cual.
    Un elemento con alternativas ("sauna | jacuzzi", "sauna o jacuzzi") se
    cumple con cualquiera de ellas.
    """
    if not condiciones:
        return []
    try:
        columnas = _columnas_filtrables()
    except Exception as e:
        return f"No se pudo leer la ficha para filtrar: {e}"
    sql: list[str] = []
    for i, condicion in enumerate(condiciones):
        alternativas = [a for a in re.split(r"\s*\|\s*|\s+o\s+", str(condicion)) if a]
        partes = []
        for j, alternativa in enumerate(alternativas):
            parte = _condicion_ficha(alternativa, f"ficha_{i}_{j}", columnas, params)
            if parte.startswith("ERROR:"):
                return parte.removeprefix("ERROR:")
            partes.append(parte)
        sql.append(partes[0] if len(partes) == 1 else f"({' OR '.join(partes)})")
    return sql


def _condicion_ficha(condicion: str, nombre: str, columnas: dict[str, str],
                     params: list) -> str:
    """SQL de una condición suelta; un error empieza por "ERROR:"."""
    m = _CONDICION_FICHA.match(condicion)
    campo = _resolver_campo_ficha(m.group("campo"), columnas) if m else None
    if campo is None:
        parecidas = difflib.get_close_matches(
            _clave_ficha(m.group("campo") if m else condicion),
            list(columnas), n=5, cutoff=0.5)
        return ("ERROR:"
                f"No hay ningún dato de la ficha llamado '{condicion}'."
                + (f" ¿Quizá: {', '.join(parecidas)}?" if parecidas else "")
                + " Usa el nombre de la columna, p. ej. 'tiene_sauna' o "
                "'num_mosquiteras >= 2'.")
    negado, op, valor = bool(m.group("neg")), m.group("op"), m.group("valor")
    tipo, col = columnas[campo], f"v.{campo}"
    if tipo == "BOOLEAN":
        if op in ("=", "!=") and valor:
            verdad = _clave_ficha(valor) in ("true", "si", "1", "yes")
            negado = negado ^ (not verdad) ^ (op == "!=")
        elif op:
            return f"ERROR:'{campo}' es sí/no: úsalo solo ('{campo}') o con 'sin {campo}'."
        return f"COALESCE({col}, FALSE) = FALSE" if negado else f"{col} = TRUE"
    if tipo in ("INTEGER", "FLOAT", "NUMERIC"):
        if not op:
            return f"COALESCE({col}, 0) = 0" if negado else f"{col} > 0"
        try:
            numero = float(valor.replace(",", "."))
        except ValueError:
            return f"ERROR:'{campo}' es un número y '{valor}' no lo es."
        params.append(bigquery.ScalarQueryParameter(nombre, "FLOAT64", numero))
        sql = f"{col} {'<>' if op == '!=' else op} @{nombre}"
        # Como en distancia_mar_m: 0 es "sin registrar", no "al lado".
        if campo.startswith("distancia_") and op in ("<", "<="):
            sql = f"({sql} AND {col} > 0)"
        return sql
    if tipo == "DATE":
        if not op:
            return f"ERROR:'{campo}' es una fecha: compárala, p. ej. '{campo} >= 2026-01-01'."
        try:
            fecha = datetime.date.fromisoformat(valor.strip())
        except ValueError:
            return f"ERROR:'{valor}' no es una fecha AAAA-MM-DD."
        params.append(bigquery.ScalarQueryParameter(nombre, "DATE", fecha))
        return f"{col} {'<>' if op == '!=' else op} @{nombre}"
    # STRING y códigos
    if not op:
        return (f"NULLIF(TRIM({col}), '') IS NULL" if negado
                else f"NULLIF(TRIM({col}), '') IS NOT NULL")
    if op in ("=", "!="):
        comparacion = "NOT LIKE" if (op == "!=") ^ negado else "LIKE"
        params.append(bigquery.ScalarQueryParameter(nombre, "STRING", f"%{valor.strip()}%"))
        return f"LOWER(COALESCE({col}, '')) {comparacion} LOWER(@{nombre})"
    return f"ERROR:'{campo}' es texto: usa '=' o '!='."


# Lo que precede al nombre de la calle y cambia de una ficha a otra ("Calle",
# "Carrer", "C/", "Urb."): se ignora para que "Calle Kabul 7" encuentre
# "Carrer Kabul, 7".
_PREFIJOS_DIRECCION = {
    "calle", "carrer", "c", "cl", "avenida", "avinguda", "avda", "av",
    "urbanizacion", "urbanización", "urb", "partida", "pda", "pdta", "cami",
    "camí", "camino", "de", "del", "la", "el", "los", "las", "en", "n", "nº",
}


def _condiciones_direccion(direccion: str, params: list) -> list[str]:
    """Cada palabra de la dirección pedida tiene que aparecer en la de la villa."""
    palabras = [
        p for p in re.findall(r"[^\W\d_]+|\d+", direccion.lower())
        if p not in _PREFIJOS_DIRECCION and (len(p) > 1 or p.isdigit())
    ]
    condiciones = []
    for i, palabra in enumerate(palabras[:6]):
        if palabra.isdigit():
            # Número entero: el 4 no puede encajar en el 14 ni en el 03724.
            condiciones.append(
                f"REGEXP_CONTAINS(v.direccion, @direccion_{i})")
            valor = rf"(^|[^0-9]){palabra}([^0-9]|$)"
        else:
            condiciones.append(f"LOWER(v.direccion) LIKE @direccion_{i}")
            valor = f"%{palabra}%"
        params.append(bigquery.ScalarQueryParameter(
            f"direccion_{i}", "STRING", valor))
    return condiciones


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
    vista_mar: bool | None = None,
    distancia_mar_max_m: int | None = None,
    zona_tranquila: bool | None = None,
    gimnasio: bool | None = None,
    accesible: bool | None = None,
    parking: bool | None = None,
    terraza: bool | None = None,
    caracteristicas: list[str] | None = None,
    direccion: str | None = None,
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
            Si NO llevan mascotas, omítelo: False dejaría fuera las
            villas que sí las admiten, que también les sirven.
        internet: True para exigir wifi/internet.
        aire_acondicionado: True para exigir aire acondicionado (dato registrado
            a nivel de salón, puede no cubrir el resto de la villa).
        lavadora: True para exigir lavadora.
        lavavajillas: True para exigir lavavajillas.
        vista_mar: True para exigir vista al mar.
        distancia_mar_max_m: Distancia máxima al mar en METROS (1000 = 1 km).
        zona_tranquila: True para zonas tranquilas.
        gimnasio: True para villas con gimnasio.
        accesible: True para villas aptas para movilidad reducida.
        parking: True para villas con algún aparcamiento (garaje, plaza
            cubierta, descubierta o en la calle).
        terraza: True para villas con terraza (cubierta o descubierta).
        caracteristicas: CUALQUIER otra característica de la villa, en
            lista: sauna, ping pong, jardín, jacuzzi, billar, garaje,
            mosquiteras, supermercado cerca, tipo de cafetera... Un
            dato sí/no por su nombre ("pingpong", "tiene_sauna", "sin
            ascensor"); un número o una fecha con comparación
            ("num_mosquiteras >= 2", "distancia_supermercado_m <= 500",
            "m2_parcela > 1000"); un texto o código con = ("tipo_cafetera_codigo
            = nespresso"). Para "X o Y" pon un solo elemento "X | Y"
            (cada elemento de la lista se exige a la vez). Los nombres son
            las columnas de la ficha (las de
            obtener_detalle_propiedad); si uno no existe, el error sugiere
            los parecidos.
        direccion: Calle, número o urbanización ("Calle Kabul 7", "Cumbre del
            Sol", "La Fustera"). Sirve también para urbanizaciones y partidas
            que no son un pueblo ni una zona. No hay dato de parcela vallada
            ni de balcón: dilo si lo piden.
        texto: Busca en nombre y tipo de villa.

    Returns:
        Diccionario con 'matches' (todas las coincidencias), 'count' y 'total'.
    """
    # activo/visible se aplican dentro del CTE, antes de deduplicar.
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if ubicacion:
        conditions.append(_condicion_lugar("v.pueblo_cercano", "ubicacion", ubicacion, params))

    if zona:
        conditions.append(_condicion_lugar("v.zona", "zona", zona, params))

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

    # Las amenidades vienen de la ficha técnica, integrada en la villa.
    conditions += _condiciones_equipamiento(
        internet=internet, aire_acondicionado=aire_acondicionado,
        lavadora=lavadora, lavavajillas=lavavajillas, vista_mar=vista_mar,
        zona_tranquila=zona_tranquila, gimnasio=gimnasio, accesible=accesible,
        parking=parking, terraza=terraza,
    )

    extra = _condiciones_ficha(caracteristicas, params)
    if isinstance(extra, str):
        return {"matches": [], "count": 0, "total": 0, "error": extra}
    conditions += extra

    if direccion:
        conditions += _condiciones_direccion(direccion, params)

    if distancia_mar_max_m is not None:
        # Sin COALESCE a propósito: si no sabemos la distancia no podemos
        # prometer que esté cerca. Y el 0 es "sin registrar", no primera
        # línea: la distancia real más pequeña del catálogo es de 30 m.
        conditions.append(
            "v.distancia_mar_m > 0 AND v.distancia_mar_m <= @distancia_mar_max_m"
        )
        params.append(bigquery.ScalarQueryParameter(
            "distancia_mar_max_m", "INT64", distancia_mar_max_m
        ))

    if texto:
        conditions.append(
            "(LOWER(v.nombre) LIKE LOWER(@texto) OR LOWER(v.tipovilla_descripcion) LIKE LOWER(@texto))"
        )
        params.append(bigquery.ScalarQueryParameter("texto", "STRING", f"%{texto.strip()}%"))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        WITH{_CTE_VILLAS_VIGENTES},{_CTE_CAMAS}
        SELECT
            v.{_columna_habitaciones()} AS numero_habitaciones,{_COLUMNAS_RESUMEN}
        FROM villa_dedup v
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
    La valoración media se calcula sobre baños, cocina, interior y exterior.
    Devuelve además, por separado, las valoraciones de vistas, privacidad,
    tranquilidad y distancia al mar.

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
        conditions.append(_condicion_lugar("pueblo_cercano", "ubicacion", ubicacion, params))

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
        WITH{_CTE_VILLAS_VIGENTES},
        base AS (
            SELECT
                v.nombre, v.pueblo_cercano, v.zona, v.region,
                v.capacidad_pax, v.{_columna_habitaciones()} AS numero_habitaciones,
                v.numero_banos, v.tiene_piscina_privada, v.admite_animales,
                v.score_rating_banos AS rating_banos,
                v.score_rating_cocina AS rating_cocina,
                v.score_rating_interior AS rating_interior,
                v.score_rating_exterior AS rating_exterior,
                v.score_rating_vistas AS rating_vistas,
                v.score_rating_privacidad AS rating_privacidad,
                v.score_rating_tranquilidad AS rating_tranquilidad,
                v.score_rating_distancia_mar AS rating_distancia_mar,{_RATING_MEDIO}
            FROM villa_dedup v
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
# La ficha técnica tiene 170 columnas. Devolverlas todas en cada consulta
# llenaría el contexto del modelo de datos que nadie ha pedido, así que por
# defecto solo va lo básico y el resto se pide por secciones.
_COLUMNAS_DETALLE_BASE = [
    "nombre", "tipovilla_nombre_comercial", "tipovilla_descripcion",
    "anio_construccion", "m2_parcela", "m2_habitables",
    "capacidad_pax", "numero_banos", "numero_plantas",
    "tiene_piscina_privada", "tiene_piscina_comun", "piscina_climatizada",
    "tiene_jardin", "tiene_garaje", "admite_animales",
    "pueblo_cercano", "zona", "region", "direccion", "latitud", "longitud",
    "propietario_nombre", "es_recomendada", "es_novedad",
]

_FICHA_BASICA = [
    "tiene_internet", "tiene_aire_acondicionado", "tiene_lavadora", "tiene_lavavajillas",
]

# Todas son columnas de stg_etendo_Villa. Fuera se quedaron zona_animada,
# zona_centrica, tiene_piscina_infantil y accesible_silla_ruedas (FALSE en
# todas las villas: nadie las rellena), distancia_farmacia_m (siempre vacía) y
# tv_satelite (era un código leído como booleano).
_SECCIONES_FICHA: dict[str, list[str]] = {
    "equipamiento": [
        "num_mosquiteras", "num_lavadoras", "tiene_secadora", "num_secadoras",
        "tiene_plancha", "ventiladores_techo", "ventiladores_pie",
    ],
    "vistas": [
        "tiene_vista_mar", "tiene_vista_panoramica_mar", "tiene_vista_despejada",
        "tiene_vista_montana", "tiene_vista_campo", "tiene_vista_limitada",
    ],
    "zona": [
        "zona_tranquila", "zona_aislada", "zona_campo",
        "urbanizacion_residencial", "cerca_del_mar",
    ],
    "distancias": [
        "distancia_mar_m", "distancia_playa_arena_m", "distancia_cala_m",
        "distancia_supermercado_m", "distancia_restaurantes_m", "distancia_golf_m",
        "distancia_tenis_m", "distancia_club_nautico_m", "distancia_hipica_m",
        "distancia_centro_medico_m", "distancia_banco_m", "distancia_pueblo_m",
        "distancia_alicante_m", "distancia_valencia_m",
        "distancia_aeropuerto_alicante_m", "distancia_aeropuerto_valencia_m",
    ],
    "piscina": [
        "piscina_largo_m", "piscina_ancho_m", "piscina_profundidad_min_m",
        "piscina_profundidad_max_m", "piscina_climatizada", "tiene_piscina_comun",
    ],
    "banos": [
        "banos_con_banera", "banos_con_ducha", "numero_aseos", "banos_en_suite",
    ],
    "estancias": [
        "salones", "salones_comedor", "comedores", "tiene_anexo", "num_anexos",
    ],
    "climatizacion": [
        "equipos_aire_acondicionado", "equipos_aire_frio", "equipos_aire_calor",
        "aire_dormitorios", "tiene_bomba_calor", "tiene_suelo_radiante",
        "tiene_chimenea", "num_chimeneas", "tiene_radiadores",
    ],
    "cocina": [
        "tiene_horno", "tiene_microondas", "tiene_congelador", "tiene_frigorifico",
        "tiene_induccion", "tiene_vitroceramica", "tiene_placa_gas",
        "tiene_placa_electrica", "tiene_gas", "cocinas_americanas",
        "cocinas_separadas", "cocinas_exteriores", "tipo_cafetera_codigo",
    ],
    "exterior": [
        "tiene_terraza_cubierta", "tiene_terraza_descubierta",
        "tiene_ducha_exterior",
        # Barbacoa de OV_Exterior: tiene_barbacoa es "de obra o portátil".
        "tiene_barbacoa", "tiene_barbacoa_obra", "tiene_barbacoa_portatil",
        "tipo_barbacoa_portatil", "gas_barbacoa", "barbacoa_plancha",
        "tiene_cesped", "tiene_arbolado",
        "tiene_solarium", "tiene_jardin",
    ],
    "ocio": [
        "tiene_gimnasio", "tiene_sauna", "tiene_jacuzzi", "tiene_hidromasaje",
        "tiene_billar", "tiene_pingpong", "tiene_pista_tenis", "tiene_pista_padel",
        "tiene_petanca", "tiene_columpio", "tiene_tobogan", "tiene_futbolin",
        "tiene_cama_elastica",
    ],
    "accesibilidad": ["apto_movilidad_reducida", "distancia_playa_adaptada_m"],
    "parking": [
        "plazas_calle", "plazas_cubiertas", "plazas_descubiertas", "plazas_garaje",
        "tiene_parking_calle", "tiene_parking_cubierto",
        "tiene_parking_descubierto", "tiene_garaje",
    ],
    "multimedia": [
        "tiene_tv", "tiene_smart_tv", "tiene_tdt", "tiene_dvd",
        "tiene_tv_satelite", "tv_satelite_codigo", "tiene_altavoz",
        "altavoz_descripcion", "tiene_amplificador_wifi", "amplificadores_wifi",
        "amplificadores_wifi_ubicacion",
    ],
    "valoraciones": [
        "score_rating_banos", "score_rating_cocina", "score_rating_interior",
        "score_rating_exterior", "score_rating_vistas", "score_rating_privacidad",
        "score_rating_tranquilidad", "score_rating_distancia_mar",
        "score_rating_equipamiento",
    ],
    "licencia": [
        "licencia_vut", "licencia_vut_normalizada", "codigo_establecimiento",
        "vut_fecha_antiguedad", "vut_fecha_caducidad", "vut_baja_registro",
        "vut_fecha_baja", "alta_ses_hospedaje", "fecha_alta_ses_hospedaje",
    ],
    "comercial": [
        "importe_fianza", "es_top_villa", "comision_pct", "esta_reformada",
        "anyo_reforma", "clasificacion", "decorador", "interiorista",
    ],
    # Códigos de alarma, ubicación de la caja fuerte, datos del wifi y puertas.
    # Solo llega a interno y admin (el rol cliente no tiene esta herramienta) y
    # solo si se pide esta sección por su nombre.
    "acceso_seguridad": [
        "tiene_alarma", "alarma_activacion", "alarma_desactivacion", "alarma_uso",
        "tiene_caja_fuerte", "caja_fuerte_ubicacion", "wifi_red", "wifi_proveedor",
        "wifi_router_ubicacion", "wifi_tipo", "wifi_num_cliente",
        "wifi_especificacion", "tipo_acceso", "acceso_coche", "puerta_parking",
        "puerta_garaje", "acceso_peatonal_desc", "escaleras", "plantas_desc",
    ],
}

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


def obtener_detalle_propiedad(
    nombre: str,
    secciones: list[str] | None = None,
) -> dict[str, Any]:
    """Devuelve la ficha completa de una propiedad específica.

    Usa cuando el usuario pregunte por una villa concreta por su nombre,
    quiera más información sobre una propiedad, o pida ver todos los detalles.
    Por defecto devuelve solo la información básica: ubicación, capacidad,
    habitaciones, camas, baños, piscina, metros y amenidades principales. La
    ficha tiene más de 150 datos; el resto se pide POR SECCIONES y solo cuando
    el usuario lo pregunte, para no llenar la respuesta de cosas que no ha
    pedido.

    Args:
        nombre: Nombre o parte del nombre de la propiedad.
        secciones: Lista de secciones extra a incluir. Solo si el usuario
            pregunta por ellas. Disponibles: equipamiento (mosquiteras,
            lavadoras, secadoras, ventiladores), vistas, zona, distancias,
            piscina, banos, estancias, climatizacion, cocina, exterior, ocio,
            accesibilidad, parking, multimedia, valoraciones, licencia,
            comercial, acceso_seguridad.
            `licencia` trae la licencia turística y su caducidad; `comercial`,
            la fianza y la comisión; `acceso_seguridad`, los códigos de alarma,
            la ubicación de la caja fuerte y los datos del wifi: pídela solo si
            te lo piden expresamente.

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
    pedidas = [s for s in (secciones or []) if s in _SECCIONES_FICHA]
    desconocidas = [s for s in (secciones or []) if s not in _SECCIONES_FICHA]
    # La ficha técnica va dentro de stg_etendo_Villa: basta con pedir sus
    # columnas en la misma consulta. Solo lo básico y las secciones pedidas.
    columnas = list(dict.fromkeys(
        _COLUMNAS_DETALLE_BASE + _FICHA_BASICA
        + [c for seccion in pedidas for c in _SECCIONES_FICHA[seccion]]
    ))
    seleccion = ",\n            ".join(f"v.{c}" for c in columnas)
    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            WHERE LOWER(nombre) LIKE LOWER(@nombre) AND es_activo = TRUE
            {_DEDUP_VILLA}
        )
        SELECT
            v.villa_id,
            v.{_columna_habitaciones()} AS numero_habitaciones,
            {seleccion}
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
        plantas = plantas_por_villa.get(villa_id)
        if plantas:
            match["plantas"] = plantas
            match.update(_totales_de_plantas(plantas))

    resultado = {
        "matches": matches,
        "count": len(matches),
        "detalle_banios_disponible": detalle_plantas_disponible,
        "detalle_dormitorios_disponible": detalle_plantas_disponible,
    }
    if desconocidas:
        resultado["secciones_ignoradas"] = desconocidas
        resultado["secciones_disponibles"] = sorted(_SECCIONES_FICHA)
    return resultado


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


# Gemini rechaza a veces una petición por falta de capacidad (429
# RESOURCE_EXHAUSTED, dos veces en una semana) o por un fallo pasajero; casi
# siempre basta con repetirla un par de segundos después. Tres intentos con
# esperas de 1 y 2 s: en el caso raro se nota, y el resto no cambia.
_REINTENTOS_GEMINI = genai_types.HttpRetryOptions(
    attempts=3,
    initial_delay=1.0,
    max_delay=4.0,
    exp_base=2.0,
    http_status_codes=[429, 500, 502, 503, 504],
)
_MODELO_GEMINI = "gemini-2.5-flash"


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
            model=_MODELO_GEMINI,
            contents=consulta.strip(),
            config=genai_types.GenerateContentConfig(
                http_options=genai_types.HttpOptions(retry_options=_REINTENTOS_GEMINI),
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


# Una villa está libre entre @fecha_desde (entrada) y @fecha_hasta (salida)
# para @noches noches si ninguna reserva la ocupa y el calendario no la
# bloquea. La comparten disponibilidad, ofertas y alternativas: si cambia la
# regla, cambia en las tres.
_SQL_VILLA_LIBRE = f"""
  AND NOT EXISTS (
      SELECT 1
      FROM {TABLA_RESERVAS} r
      WHERE r.villa_id = v.villa_id
        AND COALESCE(r.es_activo, TRUE) = TRUE
        -- Ni una cancelada ni una perdida (presupuesto que no se
        -- cerró) ocupan la villa.
        AND UPPER(COALESCE(r.estado_reserva, '')) NOT IN (
            'CA', 'CANCELACION', 'CANCELADA', 'PE', 'PERDIDA'
        )
        AND UPPER(COALESCE(r.estado_documento, '')) NOT IN (
            'VO', 'ANULADA', 'ANULADO'
        )
        AND r.fecha_entrada < @fecha_hasta
        -- Sin fecha de salida se asume que ocupa al menos la noche
        -- de entrada: dar por libre una villa ocupada es peor error.
        AND (r.fecha_salida > @fecha_desde
             OR (r.fecha_salida IS NULL
                 AND r.fecha_entrada >= @fecha_desde))
  )
  -- El calendario también bloquea: uso del propietario, cierres y
  -- ocupaciones que no están en Reserva. Un día sin estado conocido
  -- cuenta como no libre. La estancia mínima la marca el día de
  -- entrada.
  AND NOT EXISTS (
      SELECT 1
      FROM {TABLA_OCUPACION} o
      WHERE o.villa_id = v.villa_id
        AND o.es_activo = TRUE
        AND o.fecha >= @fecha_desde
        AND o.fecha < @fecha_hasta
        AND (
            UPPER(COALESCE(o.tipo_ocupacion, '')) != 'LIBRE'
            OR (o.fecha = @fecha_desde
                AND COALESCE(o.estancia_minima_noches, 0) > @noches)
        )
  )
"""


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
    admite_animales: bool | None = None,
    vista_mar: bool | None = None,
    parking: bool | None = None,
    caracteristicas: list[str] | None = None,
    direccion: str | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    """Busca villas libres en un periodo futuro y devuelve el total exacto.

    Esta es la única herramienta que debe usarse para afirmar que una villa
    está disponible o para contar villas libres. Comprueba solapamientos con
    reservas reales, los bloqueos del calendario (uso del propietario, cierres)
    y la estancia mínima del día de entrada, y rechaza fechas pasadas.

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
        admite_animales: True para villas que admiten mascotas.
            Si NO llevan mascotas, omítelo: False dejaría fuera las
            villas que sí las admiten, que también les sirven.
        vista_mar: True para exigir vista al mar.
        parking: True para villas con algún aparcamiento.
        caracteristicas: CUALQUIER otra característica de la villa (sauna,
            ping pong, jardín, jacuzzi, mosquiteras...; ver
            buscar_propiedades), p. ej. ["pingpong", "num_mosquiteras >= 2"].
        direccion: Calle o urbanización (ver buscar_propiedades).
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
        bigquery.ScalarQueryParameter("noches", "INT64", (hasta - desde).days),
    ]
    if villa_nombre:
        conditions.append("LOWER(v.nombre) LIKE LOWER(@villa_nombre)")
        params.append(bigquery.ScalarQueryParameter(
            "villa_nombre", "STRING", f"%{villa_nombre.strip()}%"
        ))
    if ubicacion:
        conditions.append(_condicion_lugar("v.pueblo_cercano", "ubicacion", ubicacion, params))
    if zona:
        conditions.append(_condicion_lugar("v.zona", "zona", zona, params))
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
    if admite_animales is not None:
        conditions.append(
            f"v.admite_animales = {'TRUE' if admite_animales else 'FALSE'}"
        )
    conditions += _condiciones_equipamiento(
        vista_mar=vista_mar, parking=parking,
    )
    extra = _condiciones_ficha(caracteristicas, params)
    if isinstance(extra, str):
        return {"available": False, "total_disponibles": 0, "matches": [],
                "error": extra}
    conditions += extra
    if direccion:
        conditions += _condiciones_direccion(direccion, params)

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
                v.admite_animales,{_COLUMNAS_DISTANCIA}
            FROM villa_dedup v
            LEFT JOIN camas c ON c.villa_id = v.villa_id
            WHERE {where}
              {_sql_villa_libre()}
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
                # Cruza con Ocupacion, que es de grano diario.
                maximum_bytes_billed=_BILLING_CAP_DIARIO,
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


def _sql_villa_libre(desde: str = "@fecha_desde", hasta: str = "@fecha_hasta",
                     noches: str = "@noches") -> str:
    """_SQL_VILLA_LIBRE con otras fechas: parámetros o columnas de la consulta."""
    return (_SQL_VILLA_LIBRE.replace("@fecha_desde", desde)
            .replace("@fecha_hasta", hasta).replace("@noches", noches))


def _estancia(fecha_desde: str, fecha_hasta: str | None):
    """(entrada, salida, error) de una estancia futura; salida exclusiva."""
    hoy = _ahora_local().date()
    try:
        desde = _parse_iso_date(fecha_desde, "fecha_desde")
        hasta = (_parse_iso_date(fecha_hasta, "fecha_hasta") if fecha_hasta
                 else desde + datetime.timedelta(days=1))
    except ValueError as exc:
        return None, None, str(exc)
    if desde < hoy:
        return None, None, (
            "No se puede consultar una fecha pasada. La fecha actual en "
            f"Europe/Madrid es {hoy.isoformat()}.")
    if hasta <= desde:
        return None, None, "fecha_hasta (salida) debe ser posterior a fecha_desde."
    return desde, hasta, None


# Precio de la estancia a partir de la tarifa diaria: la estándar noche a
# noche, o la de larga estancia si la estancia llega a su mínimo. La compra
# se suma igual para dar el margen (solo interno y admin tienen estas
# herramientas).
_SQL_TARIFA_ESTANCIA = f"""
    SELECT
        o.villa_id,
        COUNTIF(t.tipo != 'lt' AND t.es_venta) AS noches_con_precio,
        SUM(IF(t.tipo != 'lt' AND t.es_venta, t.precio_final, 0)) AS venta_std,
        SUM(IF(t.tipo != 'lt' AND NOT t.es_venta, t.precio_final, 0)) AS compra_std,
        COUNTIF(t.tipo != 'lt' AND NOT t.es_venta) AS noches_con_compra,
        COUNTIF(t.tipo = 'lt' AND t.es_venta) AS noches_larga,
        SUM(IF(t.tipo = 'lt' AND t.es_venta, t.precio_final, 0)) AS venta_larga,
        SUM(IF(t.tipo = 'lt' AND NOT t.es_venta, t.precio_final, 0)) AS compra_larga,
        COUNTIF(t.tipo = 'lt' AND NOT t.es_venta) AS noches_con_compra_larga,
        MAX(IF(t.tipo = 'lt', t.estancia_minima_noches, NULL)) AS minimo_larga
    FROM {TABLA_OCUPACION} o
    JOIN {TABLA_TARIFA_DIA} t ON t.ocupacion_id = o.id
    WHERE o.es_activo = TRUE
      AND t.es_activo = TRUE
      AND t.nombre = '{_CONCEPTO_VILLA}'
      AND t.es_venta IS NOT NULL
      AND t.precio_final IS NOT NULL
      AND o.fecha >= @fecha_desde
      AND o.fecha < @fecha_hasta
"""

_ORDEN_OFERTAS = {
    "precio": "precio_total IS NULL, precio_total, nombre",
    "precio_desc": "precio_total IS NULL, precio_total DESC, nombre",
    "margen": "margen_total IS NULL, margen_total DESC, nombre",
    "capacidad": "capacidad_pax DESC, precio_total IS NULL, precio_total",
}


def _consultar_ofertas(desde, hasta, conditions: list[str], params: list,
                       orden_sql: str, limite: int,
                       presupuesto_max: float | None = None) -> dict[str, Any]:
    """Villas libres en la estancia, con su precio total; lanza si falla BigQuery."""
    noches = (hasta - desde).days
    params = [
        *params,
        bigquery.ScalarQueryParameter("fecha_desde", "DATE", desde),
        bigquery.ScalarQueryParameter("fecha_hasta", "DATE", hasta),
        bigquery.ScalarQueryParameter("noches", "INT64", noches),
    ]
    filtro_presupuesto = ""
    if presupuesto_max is not None:
        # Solo las que tienen precio todas las noches: sin él no se puede
        # prometer que entren en el presupuesto.
        filtro_presupuesto = ("WHERE precio_completo AND precio_total <= "
                              "@presupuesto_max")
        params.append(bigquery.ScalarQueryParameter(
            "presupuesto_max", "FLOAT64", float(presupuesto_max)))
    where = " AND ".join(conditions) if conditions else "TRUE"
    query = f"""
        WITH{_CTE_VILLAS_VIGENTES},{_CTE_CAMAS},
        libres AS (
            SELECT
                v.villa_id, v.nombre, v.pueblo_cercano, v.zona,
                v.capacidad_pax,
                v.{_columna_habitaciones()} AS numero_habitaciones,
                v.numero_banos, v.tiene_piscina_privada, v.admite_animales,
                v.tiene_vista_mar,{_COLUMNAS_DISTANCIA}
            FROM villa_dedup v
            LEFT JOIN camas c ON c.villa_id = v.villa_id
            WHERE {where}
              {_sql_villa_libre()}
        ),
        tarifas AS ({_SQL_TARIFA_ESTANCIA}
              AND o.villa_id IN (SELECT villa_id FROM libres)
            GROUP BY o.villa_id
        ),
        ofertas AS (
            SELECT
                l.*,
                COALESCE(t.noches_con_precio, 0) = @noches AS precio_completo,
                (t.noches_larga = @noches AND @noches >= t.minimo_larga)
                    AS larga_estancia,
                t.minimo_larga,
                IF(t.noches_larga = @noches AND @noches >= t.minimo_larga,
                   t.venta_larga, NULLIF(t.venta_std, 0)) AS precio_total,
                -- Sin la compra de todas las noches no hay margen: saldría
                -- igual al precio de venta.
                IF(t.noches_larga = @noches AND @noches >= t.minimo_larga,
                   IF(t.noches_con_compra_larga = @noches,
                      t.venta_larga - t.compra_larga, NULL),
                   IF(t.noches_con_compra = @noches AND t.noches_con_precio = @noches,
                      t.venta_std - t.compra_std, NULL)) AS margen_total
            FROM libres l
            LEFT JOIN tarifas t USING (villa_id)
        )
        SELECT
            * EXCEPT (villa_id),
            ROUND(SAFE_DIVIDE(precio_total, @noches), 2) AS precio_medio_noche,
            ROUND(SAFE_DIVIDE(margen_total, precio_total) * 100, 1) AS margen_pct,
            COUNT(*) OVER () AS total_resultados,
            (SELECT COUNT(*) FROM ofertas) AS total_libres
        FROM ofertas
        {filtro_presupuesto}
        ORDER BY {orden_sql}
        LIMIT {min(max(1, limite), 50)}
    """
    rows = [_row_to_dict(r) for r in _bq.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=params,
            # Cruza reservas, calendario y tarifa diaria.
            maximum_bytes_billed=_BILLING_CAP_DIARIO,
        ),
    ).result()]
    total = int(rows[0]["total_resultados"]) if rows else 0
    total_libres = int(rows[0]["total_libres"]) if rows else None
    for fila in rows:
        fila.pop("total_resultados", None)
        fila.pop("total_libres", None)
        for campo in ("precio_total", "margen_total"):
            fila[campo] = _redondear(fila.get(campo))
    return {"matches": rows, "total": total, "total_libres": total_libres,
            "periodo": {"entrada": desde.isoformat(), "salida": hasta.isoformat(),
                        "noches": (hasta - desde).days}}


def buscar_ofertas(
    fecha_desde: str,
    fecha_hasta: str,
    ubicacion: str | None = None,
    zona: str | None = None,
    capacidad_min: int | None = None,
    habitaciones_min: int | None = None,
    piscina: bool | None = None,
    admite_animales: bool | None = None,
    vista_mar: bool | None = None,
    parking: bool | None = None,
    caracteristicas: list[str] | None = None,
    direccion: str | None = None,
    presupuesto_max: float | None = None,
    orden: str = "precio",
    limite: int = 10,
) -> dict[str, Any]:
    """Villas LIBRES en unas fechas CON el precio total de la estancia.

    Es la herramienta para Reservas y venta: responde en una sola llamada
    "qué tengo en Moraira del 3 al 10 de octubre para 6, con mascotas, por
    menos de 4.000 €". Úsala siempre que pregunten disponibilidad junto con
    precio, presupuesto o "qué opciones hay"; no encadenes
    consultar_disponibilidad + consultar_precios villa a villa.

    La disponibilidad sigue las mismas reglas que consultar_disponibilidad
    (reservas, bloqueos del calendario y estancia mínima). El precio es la
    suma de la tarifa de venta de cada noche, o la de larga estancia si la
    estancia llega a su mínimo; no incluye extras (energía, aire).

    Args:
        fecha_desde: Día de entrada (YYYY-MM-DD).
        fecha_hasta: Día de salida (YYYY-MM-DD, no se cobra esa noche).
        ubicacion: Pueblo cercano.
        zona: Zona geográfica.
        capacidad_min: Personas mínimas.
        habitaciones_min: Habitaciones mínimas.
        piscina: True para exigir piscina privada.
        admite_animales: True si viajan con mascotas.
            Si NO llevan mascotas, omítelo: False dejaría fuera las
            villas que sí las admiten, que también les sirven.
        vista_mar: True para exigir vistas al mar.
        parking: True para exigir algún aparcamiento.
        caracteristicas: Cualquier otra característica de la ficha, como en
            buscar_propiedades ("sauna", "jardín", "num_mosquiteras >= 2").
        direccion: Calle o urbanización.
        presupuesto_max: Precio TOTAL máximo de la estancia en euros. Deja
            fuera las villas sin precio para todas las noches.
        orden: 'precio' (defecto, más barata primero), 'precio_desc',
            'margen' (más margen primero) o 'capacidad'.
        limite: Cuántas ofertas mostrar (defecto 10, máx. 50).

    Returns:
        'matches' (villas con precio_total, precio_medio_noche,
        larga_estancia, margen_total, margen_pct, precio_completo y sus
        datos), 'total' (las que cumplen todo), 'total_libres' (libres antes
        del presupuesto) y 'periodo'. Si ninguna entra en el presupuesto pero
        hay libres, trae 'fuera_de_presupuesto' (las 3 más baratas) y un
        'aviso'. Si 'total' es 0, usa alternativas_villa o relaja un filtro
        y dilo.
    """
    desde, hasta, error = _estancia(fecha_desde, fecha_hasta)
    if error:
        return {"matches": [], "total": 0, "error": error}
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []
    for nombre, valor, columna in (
        ("ubicacion", ubicacion, "v.pueblo_cercano"),
        ("zona", zona, "v.zona"),
    ):
        if valor:
            conditions.append(_condicion_lugar(columna, nombre, valor, params))
    if capacidad_min is not None:
        conditions.append("v.capacidad_pax >= @capacidad_min")
        params.append(bigquery.ScalarQueryParameter("capacidad_min", "INT64", capacidad_min))
    if habitaciones_min is not None:
        conditions.append(f"v.{_columna_habitaciones()} >= @habitaciones_min")
        params.append(bigquery.ScalarQueryParameter(
            "habitaciones_min", "INT64", habitaciones_min))
    if piscina is not None:
        conditions.append(f"v.tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")
    if admite_animales is not None:
        conditions.append(f"v.admite_animales = {'TRUE' if admite_animales else 'FALSE'}")
    conditions += _condiciones_equipamiento(vista_mar=vista_mar, parking=parking)
    extra = _condiciones_ficha(caracteristicas, params)
    if isinstance(extra, str):
        return {"matches": [], "total": 0, "error": extra}
    conditions += extra
    if direccion:
        conditions += _condiciones_direccion(direccion, params)
    if orden not in _ORDEN_OFERTAS:
        return {"matches": [], "total": 0, "error": (
            f"orden '{orden}' no válido: usa {', '.join(_ORDEN_OFERTAS)}.")}
    try:
        resultado = _consultar_ofertas(desde, hasta, conditions, list(params),
                                       _ORDEN_OFERTAS[orden], limite, presupuesto_max)
        if presupuesto_max is not None and not resultado["total"]:
            # Nada entra en el presupuesto: lo que un vendedor necesita saber
            # es si hay algo libre por poco más, no solo "no hay".
            fuera = _consultar_ofertas(desde, hasta, conditions, list(params),
                                       _ORDEN_OFERTAS["precio"], 3)
            if fuera["matches"]:
                resultado["fuera_de_presupuesto"] = fuera["matches"]
                barata = fuera["matches"][0]
                resultado["aviso"] = (
                    f"Ninguna entra en {presupuesto_max:,.0f} €, pero hay "
                    f"{fuera['total']} libre(s) que cumplen el resto; la más "
                    f"barata es {barata['nombre']} por {barata.get('precio_total')} €. "
                    "Ofrécelas diciendo que superan el presupuesto."
                ).replace(",", ".")
        return resultado
    except Exception as e:
        log.exception("buscar_ofertas: error en BigQuery")
        return {"matches": [], "total": 0, "error": str(e)}


# Cuántos días antes y después de la entrada pedida se buscan otras fechas
# libres de la misma villa, y cuántas se proponen.
_DIAS_OTRAS_FECHAS = 14
_MAX_OTRAS_FECHAS = 5


def alternativas_villa(
    villa_nombre: str,
    fecha_desde: str,
    fecha_hasta: str,
    capacidad_min: int | None = None,
    limite: int = 5,
) -> dict[str, Any]:
    """Si una villa está libre y, si no, qué ofrecer en su lugar.

    Úsala cuando el cliente pide una villa concreta para unas fechas
    ("¿está libre Atalaya del 3 al 10 de octubre?") o cuando una búsqueda no
    da nada y hay que proponer alternativas. Devuelve en una sola llamada:
    - villa_pedida: si está libre esas fechas y su precio total.
    - otras_fechas: estancias de la misma duración de esa villa que están
      libres, empezando hasta 14 días antes o después, con su precio.
    - matches: villas parecidas libres en esas mismas fechas (misma zona,
      capacidad suficiente y piscina si la pedida la tiene), de precio más
      parecido primero.

    Args:
        villa_nombre: Villa que pide el cliente.
        fecha_desde: Día de entrada (YYYY-MM-DD).
        fecha_hasta: Día de salida (YYYY-MM-DD).
        capacidad_min: Personas del grupo; si se omite, la capacidad de la
            villa pedida.
        limite: Cuántas villas parecidas proponer (defecto 5).
    """
    desde, hasta, error = _estancia(fecha_desde, fecha_hasta)
    if error:
        return {"matches": [], "error": error}
    noches = (hasta - desde).days
    try:
        filas = [_row_to_dict(r) for r in _bq.query(
            f"""
            WITH{_CTE_VILLAS_VIGENTES}
            SELECT v.villa_id, v.nombre AS villa_nombre, v.zona,
                   v.pueblo_cercano, v.capacidad_pax, v.tiene_piscina_privada
            FROM villa_dedup v
            WHERE LOWER(v.nombre) LIKE LOWER(@villa_nombre)
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter(
                    "villa_nombre", "STRING", f"%{villa_nombre.strip()}%")],
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result()]
        filas, nombre, candidatas = _una_sola_villa(filas, villa_nombre)
        if candidatas:
            return {"matches": [], **_error_villa_ambigua(villa_nombre, candidatas)}
        if not filas:
            return {"matches": [], "error": f"No hay ninguna villa llamada '{villa_nombre}'."}
        villa = filas[0]
        villa_id = villa["villa_id"]
        id_villa = bigquery.ScalarQueryParameter("villa_id", "STRING", villa_id)

        # Precio de la villa pedida en esas noches aunque no esté libre: es la
        # mejor referencia de lo que el cliente quiere pagar.
        referencia_sql = (
            f"(SELECT NULLIF(venta_std, 0) FROM ({_SQL_TARIFA_ESTANCIA} "
            "AND o.villa_id = @villa_id GROUP BY o.villa_id))")

        def _pedida():
            return _consultar_ofertas(desde, hasta, ["v.villa_id = @villa_id"],
                                      [id_villa], _ORDEN_OFERTAS["precio"], 1)

        def _referencia():
            filas_ref = _bq.query(
                f"SELECT {referencia_sql} AS ref",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        id_villa,
                        bigquery.ScalarQueryParameter("fecha_desde", "DATE", desde),
                        bigquery.ScalarQueryParameter("fecha_hasta", "DATE", hasta),
                    ],
                    maximum_bytes_billed=_BILLING_CAP_DIARIO,
                ),
            ).result()
            return next((fila.ref for fila in filas_ref), None)

        def _otras_fechas():
            return [_row_to_dict(r) for r in _bq.query(
                f"""
                WITH{_CTE_VILLAS_VIGENTES},
                candidatas AS (
                    SELECT d AS entrada, DATE_ADD(d, INTERVAL @noches DAY) AS salida
                    FROM UNNEST(GENERATE_DATE_ARRAY(@ini, @fin)) AS d
                    WHERE d != @fecha_desde
                ),
                precio_dia AS (
                    SELECT o.fecha,
                           MAX(IF(t.tipo != 'lt' AND t.es_venta, t.precio_final, NULL)) AS venta
                    FROM {TABLA_OCUPACION} o
                    JOIN {TABLA_TARIFA_DIA} t ON t.ocupacion_id = o.id
                    WHERE o.villa_id = @villa_id AND o.es_activo = TRUE
                      AND t.es_activo = TRUE AND t.nombre = '{_CONCEPTO_VILLA}'
                      AND o.fecha >= @ini
                      AND o.fecha < DATE_ADD(@fin, INTERVAL @noches DAY)
                    GROUP BY o.fecha
                )
                SELECT c.entrada, c.salida,
                       (SELECT ROUND(SUM(p.venta), 2) FROM precio_dia p
                        WHERE p.fecha >= c.entrada AND p.fecha < c.salida) AS precio_total,
                       (SELECT COUNT(p.venta) FROM precio_dia p
                        WHERE p.fecha >= c.entrada AND p.fecha < c.salida) = @noches
                           AS precio_completo
                FROM candidatas c
                JOIN villa_dedup v ON v.villa_id = @villa_id
                WHERE TRUE
                  {_sql_villa_libre("c.entrada", "c.salida")}
                ORDER BY ABS(DATE_DIFF(c.entrada, @fecha_desde, DAY)), c.entrada
                LIMIT {_MAX_OTRAS_FECHAS}
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        id_villa,
                        bigquery.ScalarQueryParameter("fecha_desde", "DATE", desde),
                        bigquery.ScalarQueryParameter("noches", "INT64", noches),
                        bigquery.ScalarQueryParameter("ini", "DATE", max(
                            _ahora_local().date(),
                            desde - datetime.timedelta(days=_DIAS_OTRAS_FECHAS))),
                        bigquery.ScalarQueryParameter(
                            "fin", "DATE", desde + datetime.timedelta(days=_DIAS_OTRAS_FECHAS)),
                    ],
                    maximum_bytes_billed=_BILLING_CAP_DIARIO,
                ),
            ).result()]

        def _similares():
            conditions = ["v.villa_id != @villa_id", "v.capacidad_pax >= @capacidad_min"]
            params = [id_villa, bigquery.ScalarQueryParameter(
                "capacidad_min", "INT64", capacidad_min or villa.get("capacidad_pax") or 1)]
            if villa.get("zona"):
                conditions.append("LOWER(v.zona) = LOWER(@zona_ref)")
                params.append(bigquery.ScalarQueryParameter("zona_ref", "STRING", villa["zona"]))
            if villa.get("tiene_piscina_privada"):
                conditions.append("v.tiene_piscina_privada = TRUE")
            # Precio más parecido primero; sin referencia, más barata primero.
            orden_sql = (f"precio_total IS NULL, ABS(precio_total - COALESCE("
                         f"{referencia_sql}, 0)), nombre")
            return _consultar_ofertas(desde, hasta, conditions, params, orden_sql, limite)

        # Son independientes: en paralelo tarda lo que la más lenta, no la suma.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            f_pedida, f_ref = pool.submit(_pedida), pool.submit(_referencia)
            f_otras, f_similares = pool.submit(_otras_fechas), pool.submit(_similares)
            pedida, referencia = f_pedida.result(), f_ref.result()
            otras, similares = f_otras.result(), f_similares.result()

        libre = bool(pedida["matches"])
        villa_pedida = {
            "nombre": nombre, "zona": villa.get("zona"),
            "pueblo_cercano": villa.get("pueblo_cercano"),
            "capacidad_pax": villa.get("capacidad_pax"),
            "libre": libre,
            **({k: pedida["matches"][0].get(k) for k in (
                "precio_total", "precio_medio_noche", "larga_estancia",
                "margen_total", "margen_pct", "precio_completo")} if libre else {}),
        }
        otras = sorted(otras, key=lambda f: str(f.get("entrada")))
    except Exception as e:
        log.exception("alternativas_villa: error en BigQuery")
        return {"matches": [], "error": str(e)}

    return {
        "villa_pedida": villa_pedida,
        "precio_referencia": _redondear(referencia) if referencia else None,
        "otras_fechas": otras,
        "matches": similares["matches"],
        "total_similares": similares["total"],
        "periodo": similares["periodo"],
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


def _una_sola_villa(rows: list[dict], villa_nombre: str):
    """Deja solo las filas de una villa, o dice que el nombre es ambiguo.

    El nombre se busca con LIKE, así que "ADORA" casa también con "ADORABLE".
    Precios y calendario son por fecha: mezclar dos villas pisaba los precios
    del mismo día y fundía los calendarios sin ningún aviso.

    Returns:
        (filas, nombre_villa, villas_coincidentes). Si el nombre es ambiguo,
        filas va vacía y villas_coincidentes trae los candidatos.
    """
    villas = dict.fromkeys(
        (f.get("villa_id"), f.get("villa_nombre")) for f in rows if f.get("villa_id")
    )
    if len(villas) <= 1:
        return rows, next(iter(villas), (None, None))[1], None

    pedido = villa_nombre.strip().upper()
    exactas = [v for v in villas if (v[1] or "").strip().upper() == pedido]
    if len(exactas) == 1:
        villa_id, nombre = exactas[0]
        return [f for f in rows if f.get("villa_id") == villa_id], nombre, None
    return [], None, sorted({nombre or "" for _, nombre in villas})


def _error_villa_ambigua(villa_nombre: str, candidatas: list[str]) -> dict[str, Any]:
    return {
        "error": (
            f"'{villa_nombre}' coincide con {len(candidatas)} villas. "
            "Pregunta al usuario cuál quiere y repite con el nombre exacto."
        ),
        "villas_coincidentes": candidatas,
    }


def consultar_precios(
    villa_nombre: str,
    fecha_desde: str | None = None,
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
        fecha_desde: Primera noche (YYYY-MM-DD). Si se omite, se toman los
            próximos 30 días desde hoy, para poder responder "cuánto cuesta
            esta villa" sin pedirle fechas al usuario.
        fecha_hasta: Última noche (YYYY-MM-DD), incluida. Para una estancia
            con fecha de salida, pasa el día anterior a la salida: la noche
            de salida no se cobra. El rango no puede superar 92 días.

    Returns:
        Diccionario con 'noches' (venta, compra y margen por fecha),
        'resumen' del periodo, 'larga_estancia' y 'extras'.
    """
    if not fecha_desde:
        hoy = _ahora_local().date()
        fecha_desde = hoy.isoformat()
        fecha_hasta = fecha_hasta or (
            hoy + datetime.timedelta(days=_DIAS_PRECIO_POR_DEFECTO)
        ).isoformat()
    try:
        desde, hasta = _rango_valido(fecha_desde, fecha_hasta)
    except ValueError as exc:
        return {"noches": [], "resumen": {}, "error": str(exc)}

    query = f"""
        WITH{_CTE_VILLAS_VIGENTES},
        ocupacion AS (
            SELECT o.id, o.fecha, o.estado, v.villa_id, v.nombre AS villa_nombre
            FROM {TABLA_OCUPACION} o
            JOIN villa_dedup v ON v.villa_id = o.villa_id
            WHERE o.es_activo = TRUE
              AND o.fecha BETWEEN @desde AND @hasta
              AND LOWER(v.nombre) LIKE LOWER(@villa_nombre)
        )
        SELECT
            oc.villa_id, oc.villa_nombre,
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

    rows, villa, candidatas = _una_sola_villa(rows, villa_nombre)
    if candidatas:
        return {"noches": [], "resumen": {},
                **_error_villa_ambigua(villa_nombre, candidatas)}
    resultado = _componer_precios(rows, desde, hasta)
    if villa:
        resultado["villa"] = villa
    return resultado


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
        SELECT v.villa_id, v.nombre AS villa_nombre,
               o.fecha, o.tipo_ocupacion, o.reserva_id, o.estancia_minima_noches
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

    rows, villa, candidatas = _una_sola_villa(rows, villa_nombre)
    if candidatas:
        return {"tramos": [], "resumen": {},
                **_error_villa_ambigua(villa_nombre, candidatas)}
    resultado = _componer_calendario(rows, desde, hasta)
    if villa:
        resultado["villa"] = villa
    return resultado


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


def _condicion_localizador(localizador: str, params: list) -> str:
    """El localizador de Etendo es "AAAA_número"; Reservas lo escribe "2026/3079",
    "2026-3079" o solo "3079". Solo el número vale para cualquier año."""
    partes = re.findall(r"[0-9A-Za-z]+", localizador)
    if len(partes) == 1 and partes[0].isdigit():
        params.append(bigquery.ScalarQueryParameter(
            "localizador", "STRING", rf"(^|_){partes[0]}$"))
        return "REGEXP_CONTAINS(r.localizador, @localizador)"
    params.append(bigquery.ScalarQueryParameter("localizador", "STRING", "_".join(partes)))
    return "r.localizador = @localizador"


# Nombre del titular sin tildes y en minúsculas, igual que las palabras buscadas.
_TITULAR_NORMALIZADO = (
    "REGEXP_REPLACE(NORMALIZE(LOWER(COALESCE(r.cliente_nombre, '')), NFD), r'\\p{M}', '')")


def _condiciones_titular(titular: str, params: list) -> list[str]:
    """Cada palabra tiene que estar en el titular ("APELLIDOS, Nombre"), en
    cualquier orden y sin importar tildes."""
    sin_tildes = unicodedata.normalize("NFKD", titular).encode("ascii", "ignore").decode()
    palabras = [p for p in re.findall(r"[a-z0-9]+", sin_tildes.lower()) if len(p) > 1]
    condiciones = []
    for i, palabra in enumerate(palabras[:6]):
        condiciones.append(f"{_TITULAR_NORMALIZADO} LIKE @titular_{i}")
        params.append(bigquery.ScalarQueryParameter(f"titular_{i}", "STRING", f"%{palabra}%"))
    return condiciones


def detalle_reserva(localizador: str) -> dict[str, Any]:
    """Todo lo de una reserva: datos, titular, notas y plan de pagos.

    Úsala cuando ya se sabe qué reserva es (por su número, o tras localizarla
    con consultar_reservas) y piden su información: quién es el titular y cómo
    contactarle, cuántas personas y mascotas, notas de entrada y salida,
    condición de pago, cuánto hay pagado y qué plazos quedan pendientes.

    Args:
        localizador: Número de reserva ("2026_3079", "2026/3079" o "3079").

    Returns:
        'reserva' con sus datos y los del titular (titular_*), 'pagos' con
        cada plazo, 'resumen_pagos' (previsto, pagado, pendiente) y 'lineas'
        (conceptos facturados; solo existen para reservas hasta 2023). Si el
        número coincide con varias reservas, 'error' y 'candidatas'.
    """
    params: list = []
    condicion = _condicion_localizador(localizador, params)
    try:
        filas = [_row_to_dict(f) for f in _bq.query(
            f"""
            WITH villa_dedup AS (
                SELECT * FROM {TABLA_VILLA}
                {_DEDUP_VILLA}
            )
            SELECT
                r.reserva_id, r.localizador, r.villa_nombre, v.pueblo_cercano,
                r.fecha_pedido, r.fecha_confirmacion, r.fecha_entrada, r.fecha_salida,
                DATE_DIFF(r.fecha_salida, r.fecha_entrada, DAY) AS noches,
                r.adultos, r.ninos, r.num_mascotas,
                r.subtipo_reserva, r.estado_reserva, r.estado_documento,
                r.fecha_anulacion, r.es_prereserva, r.es_alto_riesgo,
                r.es_cliente_nuevo, r.estado_limpieza,
                r.importe_total, r.moneda_id, c.condicion_pago,
                r.nota_entrada, r.nota_salida, r.nota_cliente,
                r.cliente_nombre AS titular,
                t.email AS titular_email, t.nif AS titular_nif,
                t.pais AS titular_pais, t.idioma AS titular_idioma
            FROM {TABLA_RESERVAS} r
            LEFT JOIN villa_dedup v ON v.villa_id = r.villa_id
            LEFT JOIN {TABLA_TERCERO} t ON t.id = r.cliente_id
            LEFT JOIN {TABLA_CONDICION_PAGO} c ON c.id = r.condicion_pago_id
            WHERE {condicion}
            ORDER BY r.fecha_entrada DESC
            LIMIT 6
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params, maximum_bytes_billed=_BILLING_CAP),
        ).result()]
    except Exception as e:
        log.exception("detalle_reserva: error en BigQuery")
        return {"error": str(e)}

    if not filas:
        return {"error": f"No hay ninguna reserva con el número '{localizador}'."}
    if len(filas) > 1:
        return {
            "error": (f"El número '{localizador}' coincide con varias reservas; "
                      "pregunta cuál es (el número completo lleva el año)."),
            "candidatas": [
                {k: f.get(k) for k in ("localizador", "villa_nombre", "titular",
                                       "fecha_entrada", "fecha_salida", "estado_reserva")}
                for f in filas
            ],
        }
    reserva = filas[0]
    reserva_id = reserva.pop("reserva_id")
    id_param = [bigquery.ScalarQueryParameter("reserva_id", "STRING", reserva_id)]

    def _pagos():
        return [_row_to_dict(f) for f in _bq.query(
            f"""
            SELECT fecha_vencimiento, importe_previsto, importe_pagado,
                   importe_pendiente, metodo_pago_nombre, fecha_pago
            FROM {TABLA_PLAN_PAGOS}
            WHERE reserva_id = @reserva_id AND es_activo = TRUE
            ORDER BY fecha_vencimiento
            """,
            # La tabla de pagos pesa más que el límite general.
            job_config=bigquery.QueryJobConfig(
                query_parameters=id_param, maximum_bytes_billed=_BILLING_CAP_DIARIO),
        ).result()]

    def _lineas():
        return [_row_to_dict(f) for f in _bq.query(
            f"""
            SELECT numero_linea, descripcion, cantidad_pedida, precio_unitario,
                   descuento_pct, importe_neto, oferta_aplicada,
                   last_minute_aplicado, early_bird_aplicado
            FROM {TABLA_LINEA_RESERVA}
            WHERE reserva_id = @reserva_id AND es_activo = TRUE
            ORDER BY numero_linea
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=id_param, maximum_bytes_billed=_BILLING_CAP),
        ).result()]

    resultado: dict[str, Any] = {"reserva": reserva}
    # Independientes: en paralelo tarda lo que la más lenta.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_pagos, f_lineas = pool.submit(_pagos), pool.submit(_lineas)
        try:
            pagos = f_pagos.result()
            resultado["pagos"] = pagos
            resultado["pagos_disponibles"] = True
            resultado["resumen_pagos"] = {
                clave: _redondear(sum(p.get(f"importe_{clave}") or 0 for p in pagos))
                for clave in ("previsto", "pagado", "pendiente")
            }
        except Exception:
            log.warning("detalle_reserva: no se pudo leer el plan de pagos", exc_info=True)
            resultado["pagos_disponibles"] = False
        try:
            resultado["lineas"] = f_lineas.result()
        except Exception:
            log.warning("detalle_reserva: no se pudieron leer las líneas", exc_info=True)
            resultado["lineas"] = []
    return resultado


def consultar_reservas(
    localizador: str | None = None,
    titular: str | None = None,
    villa_nombre: str | None = None,
    ubicacion: str | None = None,
    zona: str | None = None,
    piscina: bool | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    salida_desde: str | None = None,
    salida_hasta: str | None = None,
    activa_en: str | None = None,
    anulada_desde: str | None = None,
    anulada_hasta: str | None = None,
    estado_reserva: str | None = None,
    estado_documento: str | None = None,
    excluir_canceladas: bool = True,
    ordenar_por: str | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    """Consulta reservas individuales con fechas, importes y datos del cliente.

    Usa para preguntas sobre reservas concretas: fechas de entrada/salida,
    importe, cliente y estado. No uses la ausencia de resultados para afirmar
    disponibilidad; para ello usa consultar_disponibilidad.
    Permite cruzar con ubicación y datos de la villa.

    Cada tipo de pregunta tiene su filtro de fecha:
    - Entradas (llegadas) de un día o periodo: fecha_desde / fecha_hasta.
    - Salidas de un día o periodo: salida_desde / salida_hasta.
    Para UN día concreto ("hoy", "mañana") pon esa fecha en los dos extremos
    (desde y hasta); con uno solo salen todas las anteriores o posteriores.
    - Villas ocupadas en una fecha ("ahora mismo", "esta noche"): activa_en.
      Cuenta la reserva si esa noche la villa está ocupada (entrada <= fecha
      < salida); la noche del día de salida ya no. Incluye las estancias del
      propietario (subtipo_reserva 'Reserva Propietario', a veces de meses):
      al responder, sepáralas de las de clientes.
    - Villas con entrada y salida el mismo día (cambio de cliente): llama una
      vez con salida ese día y otra con entrada ese día, y cruza las villas.
    - Cancelaciones de un periodo ("canceladas en los últimos 30 días"):
      anulada_desde / anulada_hasta con estado_reserva='CA'. Filtran por la
      fecha en que se anuló, no por la de entrada. Sin estado_reserva salen
      también los presupuestos perdidos ('PE'), que son la mayoría de las
      anulaciones y no son reservas canceladas.

    Para localizar una reserva concreta: por número (localizador), por
    titular, por villa y por fechas de entrada o salida, combinables. Para ver
    todo lo de una reserva ya localizada (titular, pagos, notas), usa
    detalle_reserva.

    Args:
        localizador: Número de reserva: "2026_3079", "2026/3079" o solo
            "3079" (entonces sale la de cualquier año). Por número salen todos
            los estados, también perdidas y anuladas.
        titular: Nombre y/o apellidos del titular en cualquier orden y sin
            importar tildes ("Verónica García", "garcia tomas").
        villa_nombre: Nombre o parte del nombre de la villa (ej. "NASSAU").
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira…).
        zona: Zona geográfica (Benissa Costa, Moraira…).
        piscina: True para filtrar villas con piscina privada.
        fecha_desde: Fecha de ENTRADA desde (YYYY-MM-DD).
        fecha_hasta: Fecha de ENTRADA hasta (YYYY-MM-DD).
        salida_desde: Fecha de SALIDA desde (YYYY-MM-DD).
        salida_hasta: Fecha de SALIDA hasta (YYYY-MM-DD).
        activa_en: Fecha (YYYY-MM-DD) en la que la villa está ocupada.
        anulada_desde: Fecha de anulación desde (YYYY-MM-DD).
        anulada_hasta: Fecha de anulación hasta (YYYY-MM-DD).
        estado_reserva: 'RE'=reserva, 'PE'=perdida, 'CA'=cancelación,
                        'NS'=no show, 'PR'=prereserva, 'BO'=bloqueada.
        estado_documento: 'CO'=confirmado, 'DR'=borrador, 'CL'=cerrado, 'VO'=anulado.
        excluir_canceladas: Si True (defecto), excluye canceladas (CA),
            perdidas (PE) y anuladas (VO). No hace falta desactivarlo al pedir
            un estado_reserva concreto o filtrar por fecha de anulación.
        ordenar_por: "importe" para las de mayor importe primero ("las 3
            reservas más caras": ordenar_por="importe", limite=3). Sin él,
            por fecha (de entrada, de salida o de anulación según el filtro).
        limite: Máximo de reservas a listar (defecto 20, máx. 50).

    Returns:
        Diccionario con 'reservas' (como mucho `limite`), 'count' (las
        listadas) y 'total' (todas las que cumplen los filtros). Para decir
        cuántas reservas hay usa SIEMPRE 'total', nunca 'count'. Cada reserva
        trae también `fecha_anulacion` y `estado_limpieza` (el estado de
        limpieza de la villa para esa reserva: Lista, Disponible, Limpieza
        Finalizada, Limpieza en Curso, Asignar Limpieza, Repaso Limpieza,
        Estancia, No disponible).
    """
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if localizador:
        conditions.append(_condicion_localizador(localizador, params))
        # La reserva que se busca puede estar perdida o anulada.
        excluir_canceladas = False

    if titular:
        conditions += _condiciones_titular(titular, params)

    if villa_nombre:
        conditions.append("LOWER(r.villa_nombre) LIKE LOWER(@villa_nombre)")
        params.append(bigquery.ScalarQueryParameter("villa_nombre", "STRING", f"%{villa_nombre.strip()}%"))

    if ubicacion:
        conditions.append(_condicion_lugar("v.pueblo_cercano", "ubicacion", ubicacion, params))

    if zona:
        conditions.append(_condicion_lugar("v.zona", "zona", zona, params))

    if piscina is not None:
        conditions.append(f"v.tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")

    if fecha_desde:
        conditions.append("r.fecha_entrada >= @fecha_desde")
        params.append(bigquery.ScalarQueryParameter("fecha_desde", "DATE", fecha_desde))

    if fecha_hasta:
        conditions.append("r.fecha_entrada <= @fecha_hasta")
        params.append(bigquery.ScalarQueryParameter("fecha_hasta", "DATE", fecha_hasta))

    if salida_desde:
        conditions.append("r.fecha_salida >= @salida_desde")
        params.append(bigquery.ScalarQueryParameter("salida_desde", "DATE", salida_desde))

    if salida_hasta:
        conditions.append("r.fecha_salida <= @salida_hasta")
        params.append(bigquery.ScalarQueryParameter("salida_hasta", "DATE", salida_hasta))

    if activa_en:
        conditions.append("r.fecha_entrada <= @activa_en AND r.fecha_salida > @activa_en")
        params.append(bigquery.ScalarQueryParameter("activa_en", "DATE", activa_en))

    if anulada_desde:
        conditions.append("r.fecha_anulacion >= @anulada_desde")
        params.append(bigquery.ScalarQueryParameter("anulada_desde", "DATE", anulada_desde))

    if anulada_hasta:
        conditions.append("r.fecha_anulacion <= @anulada_hasta")
        params.append(bigquery.ScalarQueryParameter("anulada_hasta", "DATE", anulada_hasta))

    # Preguntar por anulaciones o por un estado concreto y a la vez excluir
    # las canceladas daba "no se encontraron" o 6 según recordara el modelo
    # desactivarlo: lo que se pide explícitamente manda.
    if anulada_desde or anulada_hasta or estado_reserva:
        excluir_canceladas = False

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
        # Una perdida es un presupuesto que no se cerró: no ocupa la villa ni
        # es una llegada o una salida real (son un tercio de la tabla).
        conditions.append(
            "UPPER(COALESCE(r.estado_reserva, '')) NOT IN "
            "('CA', 'CANCELACION', 'CANCELADA', 'PE', 'PERDIDA')"
        )
        conditions.append(
            "UPPER(COALESCE(r.estado_documento, '')) NOT IN "
            "('VO', 'ANULADA', 'ANULADO')"
        )

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limite = min(max(1, limite), 50)
    # Primero lo más relevante para la pregunta: las últimas anuladas, o las
    # salidas y ocupaciones por orden de fecha.
    if (ordenar_por or "").strip().lower() in ("importe", "importe_total", "precio"):
        orden = "r.importe_total DESC, r.fecha_entrada DESC"
    elif anulada_desde or anulada_hasta:
        orden = "r.fecha_anulacion DESC"
    elif salida_desde or salida_hasta:
        orden = "r.fecha_salida, r.villa_nombre"
    elif activa_en:
        orden = "r.villa_nombre"
    else:
        orden = "r.fecha_entrada DESC"

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
            r.fecha_anulacion,
            r.estado_limpieza,
            -- Reserva, Reserva Agencia, Reserva TTOO o Reserva Propietario.
            r.subtipo_reserva,
            r.importe_total,
            r.moneda_id,
            r.es_prereserva,
            r.es_alto_riesgo,
            r.es_cliente_nuevo,
            v.pueblo_cercano,
            v.zona,
            v.tiene_piscina_privada,
            COUNT(*) OVER () AS total_resultados
        FROM {TABLA_RESERVAS} r
        LEFT JOIN villa_dedup v ON r.villa_id = v.villa_id
        {where}
        ORDER BY {orden}
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
        return {"reservas": [], "count": 0, "total": 0, "error": str(e)}

    reservas, total = _separar_total(rows)
    return {"reservas": reservas, "count": len(reservas), "total": total}


def resumen_reservas(
    agrupar_por: str = "villa",
    villa_nombre: str | None = None,
    ubicacion: str | None = None,
    zona: str | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    solo_en_firme: bool = True,
    incluir_propietario: bool = False,
    limite: int = 20,
) -> dict[str, Any]:
    """Estadísticas agregadas de reservas: conteos, importes y noches medias.

    Usa para preguntas analíticas: cuántas reservas tiene cada villa,
    cuánto factura, qué zona factura más, qué meses tienen más actividad,
    evolución anual. Para la facturación de una villa concreta pasa
    villa_nombre (con agrupar_por='mes' o 'ano' sale su evolución).

    Args:
        agrupar_por: Dimensión: 'villa' (defecto), 'zona', 'mes', 'ano'.
        villa_nombre: Nombre o parte del nombre de una villa concreta.
        ubicacion: Filtra por pueblo cercano (Altea, Calpe, Moraira…).
        zona: Filtra por zona geográfica.
        fecha_desde: Fecha de entrada desde (YYYY-MM-DD).
        fecha_hasta: Fecha de entrada hasta (YYYY-MM-DD).
        solo_en_firme: Si True (defecto), cuenta solo reservas en firme y no
            shows: excluye canceladas, anuladas, perdidas, prerreservas y
            borradores. Pon False solo si piden expresamente incluirlas.
        incluir_propietario: Si False (defecto), no cuenta las estancias del
            propietario en su propia villa: no facturan y distorsionan el
            importe medio y las noches medias. Pon True si preguntan por el
            uso que hacen los propietarios.
        limite: Máximo de filas (defecto 20, máx. 50). Por mes o año se
            devuelven los periodos más recientes, en orden cronológico, hasta
            el mes (o año) en curso. Para incluir reservas futuras pasa
            fecha_hasta.

    Returns:
        Diccionario con 'resumen' (lista con dimension, total_reservas,
        importe_total, importe_medio, noches_medias, noches_totales,
        importe_por_noche, monedas), 'count',
        'total_grupos' (si es mayor que count, hay grupos que no se muestran)
        y, si los importes mezclan monedas, 'aviso_monedas'.
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

    if villa_nombre:
        conditions.append("LOWER(r.villa_nombre) LIKE LOWER(@villa_nombre)")
        params.append(bigquery.ScalarQueryParameter(
            "villa_nombre", "STRING", f"%{villa_nombre.strip()}%"
        ))

    if ubicacion:
        conditions.append(_condicion_lugar("v.pueblo_cercano", "ubicacion", ubicacion, params))

    if zona:
        conditions.append(_condicion_lugar("v.zona", "zona", zona, params))

    if fecha_desde:
        conditions.append("r.fecha_entrada >= @fecha_desde")
        params.append(bigquery.ScalarQueryParameter("fecha_desde", "DATE", fecha_desde))

    if fecha_hasta:
        conditions.append("r.fecha_entrada <= @fecha_hasta")
        params.append(bigquery.ScalarQueryParameter("fecha_hasta", "DATE", fecha_hasta))

    if solo_en_firme:
        # En firme = reserva o no show (el no show se cobra). Un presupuesto
        # perdido, una prerreserva o un borrador no son facturación.
        conditions.append(
            "UPPER(COALESCE(r.estado_reserva, '')) NOT IN "
            "('CA', 'CANCELACION', 'CANCELADA', 'PE', 'PERDIDA', "
            "'PR', 'PRERESERVA', 'BO', 'BORRADOR', 'BLOQUEADA')"
        )
        conditions.append(
            "UPPER(COALESCE(r.estado_documento, '')) NOT IN "
            "('VO', 'ANULADA', 'ANULADO', 'DR', 'BORRADOR')"
        )

    if not incluir_propietario:
        conditions.append(
            "COALESCE(r.subtipo_reserva, '') != 'Reserva Propietario'"
        )

    cronologico = group_key in ("mes", "ano")
    if cronologico and not fecha_hasta:
        # Hay reservas cargadas años por delante: sin tope, "los periodos más
        # recientes" eran meses futuros con un puñado de reservas.
        conditions.append(
            "r.fecha_entrada <= LAST_DAY(CURRENT_DATE('Europe/Madrid')"
            + (", YEAR)" if group_key == "ano" else ")")
        )

    where = f"WHERE {' AND '.join(conditions)}"
    limite = min(max(1, limite), 50)
    # Por tiempo interesan los periodos más recientes, no los de más volumen.
    orden = "dimension DESC" if cronologico else "total_reservas DESC"

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
            -- Hay reservas con la salida igual o anterior a la entrada: sin
            -- acotar a 0 restarían noches y dispararían el ingreso por noche.
            SUM(GREATEST(DATE_DIFF(r.fecha_salida, r.fecha_entrada, DAY), 0))
                AS noches_totales,
            ROUND(SAFE_DIVIDE(
                SUM(r.importe_total),
                SUM(GREATEST(DATE_DIFF(r.fecha_salida, r.fecha_entrada, DAY), 0))
            ), 2) AS importe_por_noche,
            MIN(r.fecha_entrada) AS primera_entrada,
            MAX(r.fecha_entrada) AS ultima_entrada,
            ARRAY_AGG(DISTINCT r.moneda_id IGNORE NULLS) AS monedas,
            COUNT(*) OVER () AS total_grupos
        FROM {TABLA_RESERVAS} r
        LEFT JOIN villa_dedup v ON r.villa_id = v.villa_id
        {where}
        GROUP BY {group_expr}
        ORDER BY {orden}
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
    total_grupos = resumen[0].get("total_grupos", len(resumen)) if resumen else 0
    monedas: set[str] = set()
    for fila in resumen:
        fila.pop("total_grupos", None)
        monedas.update(fila.get("monedas") or [])
    if cronologico:
        resumen.sort(key=lambda f: str(f.get("dimension")))

    resultado: dict[str, Any] = {
        "resumen": resumen,
        "count": len(resumen),
        "total_grupos": total_grupos,
    }
    if len(monedas) > 1:
        resultado["aviso_monedas"] = (
            "Los importes mezclan varias monedas (ver 'monedas' de cada fila): "
            "no los sumes ni compares entre sí sin advertirlo al usuario."
        )
    return resultado


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


# Referencias de tabla: `proj.dataset.tabla` entero entre acentos graves, por
# partes (`proj`.`dataset`.`tabla`) o sin ellos. Tras FROM/JOIN puede venir una
# lista separada por comas ("FROM a x, b y"), y cada elemento es una tabla.
_PARTE_ID = r"(?:`[^`]*`|[A-Za-z_][\w$-]*)"
_REF = rf"{_PARTE_ID}(?:\s*\.\s*{_PARTE_ID})*"
_ALIAS = r"(?:\s+(?:AS\s+)?[A-Za-z_]\w*)?"
_LISTA_FROM = re.compile(
    rf"\b(?:FROM|JOIN)\s+({_REF}{_ALIAS}(?:\s*,\s*{_REF}{_ALIAS})*)",
    re.IGNORECASE,
)
_COMENTARIO_SQL = re.compile(r"--[^\n]*|#[^\n]*|/\*.*?\*/", re.DOTALL)
# EXTRACT(YEAR FROM columna) lleva un FROM que no introduce ninguna tabla.
_EXTRACT_FROM = re.compile(r"\bEXTRACT\s*\(\s*\w+\s+FROM\b", re.IGNORECASE)


def _referencias_de_tabla(query: str) -> list[str]:
    texto = _EXTRACT_FROM.sub("EXTRACT(", _COMENTARIO_SQL.sub(" ", query))
    referencias = []
    for lista in _LISTA_FROM.findall(texto):
        for elemento in lista.split(","):
            encontrada = re.match(_REF, elemento.strip())
            if encontrada:
                referencias.append(encontrada.group(0))
    return referencias


def _proyecto_y_dataset(referencia: str) -> tuple[str | None, str | None]:
    partes = [t.strip() for t in referencia.replace("`", "").split(".")]
    if len(partes) < 2:                                    # CTE, alias, UNNEST
        return None, None
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

    for referencia in _referencias_de_tabla(limpia):
        proyecto, dataset = _proyecto_y_dataset(referencia)
        if dataset is None:
            continue
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


def _motivo_rechazo_dry_run(prueba) -> str | None:
    """Revisa lo que BigQuery dice que va a hacer la consulta."""
    tipo = prueba.statement_type
    if tipo and tipo.upper() != "SELECT":
        return f"Solo se permiten consultas SELECT; esta es {tipo}."
    for tabla in prueba.referenced_tables or []:
        if (tabla.project.lower() != PROJECT_ID.lower()
                or tabla.dataset_id.lower() != DATASET.lower()):
            return (
                f"Solo se puede consultar {PROJECT_ID}.{DATASET}; la consulta "
                f"lee {tabla.project}.{tabla.dataset_id}.{tabla.table_id}."
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

    # La expresión regular es un primer filtro barato, pero no entiende SQL.
    # La lista fiable de tablas la da BigQuery en un dry run, que ni lee datos
    # ni factura: vistas, subconsultas y sintaxis rara quedan cubiertas.
    try:
        prueba = _bq.query(
            query,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
    except Exception as e:
        log.warning("ejecutar_sql: la consulta no supera el dry run: %s", e)
        return {"rows": [], "count": 0, "error": str(e)}
    motivo = _motivo_rechazo_dry_run(prueba)
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
- Un filtro negativo solo si el usuario EXCLUYE algo ("que no admita
  mascotas"). "No llevan mascotas" o "no necesitan piscina" quitan el filtro,
  no lo invierten.
- En una pregunta de seguimiento sobre una LISTA de villas ("¿y cuáles tienen
  jacuzzi?", "¿y para 8?") MANTÉN los filtros de la búsqueda anterior (fechas,
  pueblo, personas, mascotas...) y añade el nuevo, con la misma herramienta:
  si antes buscabas disponibilidad, sigue con `consultar_disponibilidad`.
  Solo quita un filtro si el usuario lo pide.
- Nunca inventes datos. Si no hay resultados en BigQuery, sugiere alternativas.
- Distancias: `distancia_mar_m` es hasta la costa (puede ser una zona de
  rocas) y `distancia_playa_arena_m` hasta la playa de arena más cercana; si
  preguntan por "la playa", da las dos. Las listas de villas traen ambas; si
  una villa no la tiene, di que no consta. Una distancia solo vale si viene en
  el resultado de una herramienta.
- Muestra los datos de forma clara: nombre, ubicación, capacidad, amenidades.
- Para decir CUÁNTOS resultados hay usa el `total` (o `total_disponibles`) que
  devuelve la herramienta, nunca el número de filas que te ha enseñado.
- Si una herramienta devuelve `villas_coincidentes`, el nombre es ambiguo:
  pregunta al usuario cuál de esas villas quiere y no des datos hasta saberlo.
- Si una herramienta devuelve `error`, no presentes su respuesta como un dato:
  di que no se ha podido obtener esa información.
- NUNCA digas que solo puedes ayudar con villas si la pregunta es sobre turismo,
  fiestas, eventos o clima: usa `buscar_internet` primero.

## Mapas y gráficos
- La aplicación pinta sola un mapa con las villas que devuelvan las búsquedas
  y fichas, y gráficos con los precios, el calendario y los resúmenes de
  reservas que consultes. NUNCA digas que no puedes hacer
  mapas o gráficos: si los piden, llama a la herramienta que corresponda y di
  que el mapa o el gráfico aparece bajo la respuesta. No escribas coordenadas
  ni tablas para copiar a una hoja de cálculo en su lugar.

## Búsquedas por equipamiento y dirección
- `buscar_propiedades` y `consultar_disponibilidad` filtran por CUALQUIER
  característica de la ficha con `caracteristicas=[...]`: sauna, ping pong,
  jardín, jacuzzi, billar, garaje, mosquiteras ("num_mosquiteras >= 2"),
  distancias ("distancia_supermercado_m <= 500"), "sin ascensor"... Cada
  elemento se exige a la vez; para "sauna o jacuzzi" usa un solo elemento
  "sauna | jacuzzi". Si el
  usuario pide "villas con X", pon X en `caracteristicas` y deja que la
  herramienta diga si existe. NUNCA digas que no hay filtro para algo sin
  haberlo intentado.
- Para una calle, un número o una urbanización ("Calle Kabul 7", "Cumbre del
  Sol", "La Fustera", "El Portet") usa el parámetro `direccion`.
- Barbacoa: "barbacoa" (de obra o portátil), "barbacoa de obra", "barbacoa
  portátil" y el tipo de la portátil ("tipo_barbacoa_portatil = gas", carbón,
  piedra, eléctrica...). En la ficha también salen el gas (bombonas, tanque) y
  la plancha. De las barbacoas de obra no consta si son de carbón, gas o
  eléctricas: si lo preguntan, dilo.
- No hay dato de parcela vallada ni de balcón: dilo claramente.

## Fecha, hora y disponibilidad
- La zona horaria del negocio y del agente es `Europe/Madrid`.
- Para cualquier referencia relativa ("hoy", "mañana", "este fin de semana",
  "este mes", etc.), llama primero a `obtener_fecha_hora_actual()`. No inventes
  la fecha ni pidas al usuario un rango si su expresión relativa es suficiente.
- Para saber si una villa está libre o contar villas libres, llama SIEMPRE a
  `consultar_disponibilidad(...)`. Ninguna otra herramienta demuestra que una
  villa esté libre.
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
""".strip()

# Solo lo que tienen TODOS los roles. Mencionar aquí una herramienta que un rol
# no tiene hace que el modelo la llame, y ADK corta la conversación con
# "Tool ... not found". tests/test_criticos.py lo vigila.
_HERRAMIENTAS_COMUNES = """
- `obtener_fecha_hora_actual()`: fecha y hora actual en Europe/Madrid; obligatoria
  para expresiones relativas como hoy o mañana.
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad,
  habitaciones (`habitaciones_min`), camas reales (`camas_min`),
  vista al mar (`vista_mar`), distancia al mar en metros
  (`distancia_mar_max_m`), zona tranquila, gimnasio, accesibilidad
  (`accesible`), baños, metros habitables, piscina privada, mascotas, internet,
  aire acondicionado, lavadora, lavavajillas.
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo (escala 1-6, media de baños, cocina, interior y exterior).
- `consultar_disponibilidad(...)`: única fuente para afirmar disponibilidad o
  contar villas libres; cruza reservas, bloqueos del calendario y estancia
  mínima, y rechaza fechas pasadas.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `buscar_pagina_web(consulta)`: localiza la URL real de una página de la web
  cuando no está en la lista de páginas clave o `consultar_web` no la encuentra.
- `buscar_internet(consulta)`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…). OBLIGATORIO para esas preguntas.
""".strip()

_REGLAS_GESTION = """
## Cuánta información dar de una villa
- Si preguntan si UNA villa concreta tiene algo ("¿Alessia tiene ping
  pong?"), no es una búsqueda: mira su ficha con `obtener_detalle_propiedad`
  y la sección que toque (ocio, exterior, piscina, parking...). No digas que no
  tienes el dato sin haber mirado la ficha.
- Si preguntan por un dato de villas que ya has mostrado y ese dato no viene
  en lo que devolvió la herramienta (una distancia, un equipamiento...),
  consúltalo en la ficha de cada una con `obtener_detalle_propiedad` (para
  distancias, `secciones=["distancias"]`) y compáralas con esos datos. Nunca lo
  estimes, ni lo deduzcas de otra villa, ni digas que no lo tienes sin haber
  mirado la ficha.
- Si preguntan por una villa en general ("háblame de", "situación de", "info
  de"), responde lo BÁSICO: ubicación, capacidad, habitaciones, camas, baños,
  piscina, metros y precio. Nada más. Llama a la vez a
  `obtener_detalle_propiedad` y a `consultar_precios`: sin el precio la
  respuesta está incompleta.
- La ficha tiene más de 150 datos repartidos en secciones (equipamiento como
  mosquiteras o lavadoras, vistas, distancias, piscina, cocina, ocio,
  accesibilidad, licencia, comercial...). Pide una
  sección a `obtener_detalle_propiedad(nombre, secciones=[...])` SOLO cuando
  el usuario pregunte por ese tema. No las traigas "por si acaso".
- Da el precio en esa misma respuesta, sin preguntar antes: llama a
  `consultar_precios`, que sin fechas toma los próximos 30 días. No pidas
  fechas al usuario para dar un precio orientativo.

## Datos de acceso a las villas (alarma, caja fuerte, wifi, puertas)
- El usuario de esta versión es personal interno de Abahana y está autorizado
  a conocer los datos de acceso de las villas: instrucciones y códigos de la
  alarma, ubicación de la caja fuerte, red y proveedor del wifi, puertas y
  accesos. Los necesita para su trabajo (entradas, incidencias, mantenimiento).
- Cuando pregunte por cualquiera de ellos, llama a
  `obtener_detalle_propiedad(nombre, secciones=["acceso_seguridad"])` y dale lo
  que devuelva. Vale también para preguntas de seguimiento sobre villas ya
  mencionadas, como "¿y cuál es su contraseña?" o "¿cómo se desactiva?":
  consulta cada una de esas villas.
- No digas que no tienes acceso ni que es información confidencial: sí tienes
  acceso y este usuario puede verla. Si un campo viene vacío, di que ese dato
  no consta en la ficha de la villa.
- Etendo no guarda contraseñas de wifi ni una "contraseña" de alarma aparte:
  los códigos van dentro de las instrucciones de activación y desactivación.
- No saques estos datos si no te los piden.

## Reservas y venta: ofertas y alternativas
- Si preguntan qué hay libre en unas fechas CON precio, presupuesto u "opciones"
  ("qué tengo en Moraira del 3 al 10 para 6 con mascotas, hasta 4.000 €"),
  usa `buscar_ofertas`: da en una sola llamada las villas libres con el precio
  total de la estancia, el precio medio por noche y el margen. No encadenes
  `consultar_disponibilidad` y `consultar_precios` villa a villa.
- Si piden una villa concreta para unas fechas ("¿está libre Atalaya del 3 al
  10?"), usa `alternativas_villa`: dice si está libre y su precio, y si no, qué
  fechas cercanas tiene libres y qué villas parecidas lo están.
- Si una búsqueda no da nada, no te quedes en "no hay": propón alternativas
  (`alternativas_villa`, o repite `buscar_ofertas` relajando un filtro y di
  cuál).
- Para el precio de una estancia concreta (entrada y salida) usa el
  `precio_total` de `buscar_ofertas` o `alternativas_villa`, que ya descuenta
  la noche de salida. `consultar_precios` es noche a noche y su `fecha_hasta`
  es la última noche: si la usas para una estancia, pásale la salida menos un
  día.
- Al presentar ofertas da siempre el precio total de la estancia y por noche;
  si `larga_estancia` es true, di que se aplica la tarifa de larga estancia, y
  si `precio_completo` es false, que faltan precios de alguna noche.

## Reservas y facturación
- Para localizar una reserva usa `consultar_reservas` con lo que den: número
  (`localizador`: "2026_3079", "2026/3079" o solo "3079"), nombre y/o apellidos
  del `titular` (en cualquier orden, sin importar tildes), villa, y fechas de
  entrada o de salida, combinables. Con un número suelto pueden salir reservas
  de varios años: pregunta cuál.
- Para toda la información de una reserva ya localizada (titular y su email,
  país e idioma; personas y mascotas; notas de entrada y salida; condición de
  pago; plan de pagos con lo pagado y lo pendiente) usa `detalle_reserva`. Las
  líneas facturadas solo existen para reservas hasta 2023: si faltan, no digas
  que la reserva no tiene conceptos. Del huésped (si no es el titular) no hay
  datos.
- Para "las reservas más caras" o "las de mayor importe" usa
  `consultar_reservas(ordenar_por="importe", limite=N)` con las fechas que
  toquen; no escribas SQL para eso. Deja fuera perdidas y anuladas.
- La ausencia de filas en `consultar_reservas` no demuestra disponibilidad.
- `consultar_reservas` filtra por entrada, por salida (`salida_desde`,
  `salida_hasta`), por ocupación en una fecha (`activa_en`) y por fecha de
  anulación. Para un briefing del día consulta entradas, salidas y ocupación;
  no digas que solo puedes filtrar por fecha de entrada. Cada reserva trae su
  `estado_limpieza`.
- `resumen_reservas` cuenta por defecto solo reservas en firme (y no shows) y
  deja fuera las estancias del propietario. Si devuelve `aviso_monedas`,
  advierte de que los importes mezclan monedas.
- En `consultar_reservas`, `subtipo_reserva` = 'Reserva Propietario' es el
  propietario usando su villa, no un cliente: no lo presentes como una venta.
- No hay datos de incidencias ni de mantenimiento: la columna `incidencia` de
  las líneas de reserva está vacía y no existe otra tabla con ellos. Si
  preguntan por incidencias abiertas, dilo así en vez de deducirlas de
  reservas, estados o fichas.
- Usa `consultar_feedback_negativo()` si necesitas ver qué respuestas han fallado o
  quedado incompletas recientemente en otras conversaciones para no repetir errores.
""".strip()

_HERRAMIENTAS_GESTION = """
- `obtener_detalle_propiedad(nombre)`: ficha completa con dirección, coordenadas,
  desglose de camas, metros habitables, ratings por categoría, propietario,
  amenidades (internet, aire acondicionado, lavadora, lavavajillas) y desglose
  real de los baños (bañera, ducha, jacuzzi, bidé, en-suite). Úsala cuando el
  usuario pregunte por una villa concreta o pida más detalles.
- `buscar_ofertas(fecha_desde, fecha_hasta, ...)`: villas libres con el precio
  total de la estancia y el margen, filtradas por zona, personas,
  características y presupuesto (`presupuesto_max`), ordenadas por precio o
  por margen. Es la herramienta principal para vender.
- `alternativas_villa(villa_nombre, fecha_desde, fecha_hasta)`: si una villa
  está libre y su precio; si no, fechas cercanas libres de esa villa y villas
  parecidas libres, de precio más parecido primero.
- `consultar_precios(villa_nombre, ...)`: precio de venta, precio de compra y
  margen noche a noche, tarifa de larga estancia y extras.
- `calendario_villa(villa_nombre, fecha_desde, ...)`: calendario de ocupación de
  una villa por tramos (libre, ocupada por canal, bloqueada) y % de ocupación.
  Para saber si se puede vender en unas fechas, usa `consultar_disponibilidad`.
- `consultar_reservas(...)`: reservas individuales con fechas de entrada/salida,
  importe, cliente y estado. Filtra por número de reserva (`localizador`),
  `titular`, villa, ubicación, zona, piscina, rango de
  fechas, estado_reserva ('RE'=reserva, 'PE'=perdida, 'CA'=cancelación,
  'NS'=no show, 'PR'=prereserva, 'BO'=borrador) y estado_documento
  ('CO'=confirmado, 'DR'=borrador, 'CL'=cerrado, 'VO'=anulado). Devuelve `total`
  con el número exacto aunque solo liste unas pocas.
- `detalle_reserva(localizador)`: todo lo de una reserva: titular y su
  contacto, personas, notas, condición de pago y plan de pagos (pagado y
  pendiente).
- `resumen_reservas(...)`: estadísticas agregadas (count, importe total/medio,
  noches medias). Agrupa por 'villa', 'zona', 'mes' o 'ano'. Para preguntas
  analíticas: qué villa tiene más reservas, qué zona factura más, evolución mensual.
- `consultar_feedback_negativo()`: respuestas parciales o no resueltas recientemente, con motivos.
- `listar_tablas_disponibles()` / `describir_tabla(nombre_tabla)` / `ejecutar_sql(query)`:
  úsalas SOLO si ninguna herramienta anterior cubre la pregunta, o si una tabla fija
  parece haber cambiado de nombre/columnas. Explora primero con las dos primeras antes
  de escribir la query. Ten en cuenta las trampas documentadas en `ejecutar_sql`
  (duplicados en stg_etendo_Villa, join por villa_id en Planta/Banio).
""".strip()

INSTRUCTION_CLIENTE = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
{_HERRAMIENTAS_COMUNES}

## Precios y reservas
- No tienes acceso a tarifas, precios ni reservas. Si preguntan cuánto cuesta
  una villa, dilo con claridad y remite a la ficha de la villa en la web o a la
  página de contacto. No des ni estimes cifras.
""".strip()

INSTRUCTION_INTERNO = f"""{_INSTRUCCION_BASE}

{_REGLAS_GESTION}

## Herramientas disponibles
{_HERRAMIENTAS_COMUNES}
{_HERRAMIENTAS_GESTION}

## Contexto de uso interno
Eres la versión para agentes de ventas y equipo interno. Puedes mostrar la dirección
completa, coordenadas, datos de reservas, importes, precios de compra y márgenes.
""".strip()

INSTRUCTION_ADMIN = f"""{_INSTRUCCION_BASE}

{_REGLAS_GESTION}

## Herramientas disponibles
{_HERRAMIENTAS_COMUNES}
{_HERRAMIENTAS_GESTION}

## Contexto de uso
Eres la versión de administración. Tienes acceso completo a todos los datos disponibles.
""".strip()


# ---------------------------------------------------------------------------
# Salvaguardas comunes a todas las herramientas
# ---------------------------------------------------------------------------

def _admite_solo_texto(anotacion: Any) -> bool:
    """True si el parámetro es `str` o `str | None`."""
    if anotacion is str:
        return True
    if typing.get_origin(anotacion) in (typing.Union, types.UnionType):
        return set(typing.get_args(anotacion)) == {str, type(None)}
    return False


def normalizar_argumentos(tool, args: dict[str, Any], tool_context) -> dict | None:
    """Ajusta lo que el modelo pasa a un parámetro de texto antes de llamar.

    Quita también el "Villa" delante del nombre de una villa.

    El modelo a veces manda una lista donde va un texto (varias villas en
    `nombre`, varios estados a la vez) o un número; la herramienta hacía
    `.strip()` y la excepción tumbaba la respuesta entera ("'list' object
    has no attribute 'strip'"). Una lista de un elemento se desenvuelve; con
    varios se le pide que llame una vez por valor, que es lo que la
    herramienta sabe hacer.
    """
    func = getattr(tool, "func", None)
    if func is None:
        return None
    try:
        tipos = typing.get_type_hints(func)
    except Exception:
        return None
    for nombre, valor in list(args.items()):
        if not _admite_solo_texto(tipos.get(nombre)):
            continue
        if isinstance(valor, (list, tuple)):
            valores = [str(v) for v in valor if v not in (None, "")]
            if len(valores) > 1:
                return {"error": (
                    f"El parámetro '{nombre}' de {tool.name} admite un solo valor "
                    f"y has pasado {len(valores)}: {', '.join(valores)}. Llama a "
                    f"{tool.name} una vez por cada uno."
                )}
            args[nombre] = valores[0] if valores else None
        elif isinstance(valor, (int, float)) and not isinstance(valor, bool):
            args[nombre] = str(valor)
    # "Villa Atalaya" no encaja en '%Villa Atalaya%': en Etendo se llama
    # ATALAYA, y consultar_precios devolvía 0 noches. Las dos que sí se llaman
    # "VILLA ..." siguen encontrándose, porque la búsqueda es por subcadena.
    for nombre in ("villa_nombre", "nombre"):
        valor = args.get(nombre)
        if isinstance(valor, str) and re.match(r"(?i)\s*villa\s+\S", valor):
            args[nombre] = re.sub(r"(?i)^\s*villa\s+", "", valor)
    return None


def error_de_herramienta(tool, args: dict[str, Any], tool_context,
                         error: Exception) -> dict:
    """Un fallo de una herramienta llega al modelo como dato, no como excepción.

    Sin esto, cualquier error dentro de una herramienta cortaba el turno y el
    usuario solo veía "Error: ..." sin respuesta; así el modelo puede probar
    otra vía o explicar qué dato no ha podido obtener.
    """
    log.warning("Falló la herramienta %s con %s", tool.name, args, exc_info=error)
    return {"error": (
        f"La herramienta {tool.name} ha fallado ({type(error).__name__}: "
        f"{str(error)[:300]}). No inventes el dato: prueba otra herramienta si "
        "tiene sentido o explica al usuario qué no has podido consultar."
    )}


_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES_LARGOS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                 "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def fecha_de_hoy(callback_context, llm_request) -> None:
    """Pone la fecha de hoy en las instrucciones de cada llamada al modelo.

    Sin razonamiento previo, el modelo ya no llamaba a obtener_fecha_hora_actual
    antes de interpretar "del 3 al 10 de octubre" y ponía 2024 por su cuenta:
    "no puedo consultar fechas pasadas". Con la fecha delante no hace falta
    esa llamada, y además es un paso del modelo menos.
    """
    llm_request.append_instructions([_texto_fecha_de_hoy(_ahora_local())])
    return None


def _texto_fecha_de_hoy(hoy: datetime.datetime) -> str:
    """La fecha de hoy y a qué año pertenece cada mes si no lo dicen.

    "La próxima vez que llega ese día" no bastó: el modelo seguía poniendo
    2024 o 2027 a "del 3 al 10 de octubre". Con el año de cada mes escrito
    no tiene que deducir nada.
    """
    este, siguiente = hoy.year, hoy.year + 1
    meses_este = _MESES_LARGOS[hoy.month - 1:]
    meses_siguiente = _MESES_LARGOS[:hoy.month - 1]
    anios = (f"{meses_este[0]} (desde hoy) es de {este}" if len(meses_este) == 1
             else f"{meses_este[0]} (desde hoy) a {meses_este[-1]} son de {este}")
    if meses_siguiente:
        anios += f"; {meses_siguiente[0]} a {meses_siguiente[-1]}, de {siguiente}"
    ejemplo_mes = _MESES_LARGOS[hoy.month % 12]
    ejemplo_anio = este if hoy.month < 12 else siguiente
    return (
        f"## Hoy\nHoy es {_DIAS[hoy.weekday()]} {hoy.day} de "
        f"{_MESES_LARGOS[hoy.month - 1]} de {este} ({hoy.date().isoformat()}), "
        f"{hoy:%H:%M} en Europe/Madrid.\n"
        f"Si el usuario no dice el año: {anios}. Por ejemplo, \"del 3 al 10 "
        f"de {ejemplo_mes}\" es del {ejemplo_anio}-{hoy.month % 12 + 1:02d}-03 al "
        f"{ejemplo_anio}-{hoy.month % 12 + 1:02d}-10. Un día de "
        f"{_MESES_LARGOS[hoy.month - 1]} anterior a hoy es de {siguiente}. "
        "Nunca uses una fecha pasada."
    )


# ---------------------------------------------------------------------------
# Agentes por rol
# ---------------------------------------------------------------------------

# Sin razonamiento previo (thinking) en cada paso del modelo. Medido con
# preguntas reales del histórico: 7,9 s de media por respuesta con el de por
# defecto, 8,2 s con 512 tokens y 4,5 s sin él, y sin respuestas peores (con
# razonamiento llegó a filtrar mal las salidas de hoy). Una respuesta con dos
# herramientas son tres pasos del modelo, y cada uno pensaba antes.
_CONFIG_MODELO = genai_types.GenerateContentConfig(
    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
)

agent_cliente = Agent(
    name="abahana_villas_agent_cliente",
    model=Gemini(model=_MODELO_GEMINI, retry_options=_REINTENTOS_GEMINI),
    generate_content_config=_CONFIG_MODELO,
    before_model_callback=fecha_de_hoy,
    description=(
        "Asistente de Abahana Villas: villas vacacionales, web corporativa "
        "e información turística local (fiestas, eventos, clima)."
    ),
    instruction=INSTRUCTION_CLIENTE,
    # Sin consultar_feedback_negativo: devuelve preguntas y respuestas de otros
    # usuarios, que no deben llegar a un cliente.
    before_tool_callback=normalizar_argumentos,
    on_tool_error_callback=error_de_herramienta,
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
    model=Gemini(model=_MODELO_GEMINI, retry_options=_REINTENTOS_GEMINI),
    generate_content_config=_CONFIG_MODELO,
    before_model_callback=fecha_de_hoy,
    description=(
        "Asistente interno de Abahana Villas: villas, fichas completas, reservas, "
        "web corporativa e información turística local (fiestas, eventos, clima)."
    ),
    instruction=INSTRUCTION_INTERNO,
    before_tool_callback=normalizar_argumentos,
    on_tool_error_callback=error_de_herramienta,
    tools=[
        obtener_fecha_hora_actual,
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
        consultar_disponibilidad,
        buscar_ofertas,
        alternativas_villa,
        consultar_reservas,
        detalle_reserva,
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
    model=Gemini(model=_MODELO_GEMINI, retry_options=_REINTENTOS_GEMINI),
    generate_content_config=_CONFIG_MODELO,
    before_model_callback=fecha_de_hoy,
    description=(
        "Asistente de administración de Abahana Villas: villas, fichas completas, "
        "reservas, web corporativa e información turística local (fiestas, eventos, clima)."
    ),
    instruction=INSTRUCTION_ADMIN,
    before_tool_callback=normalizar_argumentos,
    on_tool_error_callback=error_de_herramienta,
    tools=[
        obtener_fecha_hora_actual,
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
        consultar_disponibilidad,
        buscar_ofertas,
        alternativas_villa,
        consultar_reservas,
        detalle_reserva,
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
