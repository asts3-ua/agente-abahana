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
import logging
import os
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
TABLA_FICHA_TECNICA = f"`{PROJECT_ID}.{DATASET}.stg_etendo_opxdes_ficha_tecnica`"

# stg_etendo_Villa trae filas duplicadas por villa_id (misma villa varias veces).
# Este filtro se aplica siempre para quedarnos con una sola fila por villa.
_DEDUP_VILLA = (
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY villa_id ORDER BY fecha_actualizacion DESC) = 1"
)

_bq = bigquery.Client(project=PROJECT_ID, location="EU")
_BILLING_CAP = 50 * 1024 * 1024  # 50 MB — tablas silver_clean materializadas
_TIMEZONE = ZoneInfo("Europe/Madrid")
_ESTADOS_RESERVA = {
    "RE": "RESERVA",
    "PE": "PERDIDA",
    "CA": "CANCELACION",
    "NS": "NOSHOW",
    "PR": "PRERESERVA",
    "BO": "BORRADOR",
}
_ESTADOS_DOCUMENTO = {
    "CO": "COMPLETADA",
    "CL": "CERRADA",
    "DR": "BORRADOR",
    "VO": "ANULADA",
}


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
    """Devuelve todas las propiedades disponibles con sus características.

    Usa cuando el usuario pida ver el catálogo completo o todas las villas
    sin especificar filtros.
    """
    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            WHERE es_activo = TRUE AND es_visible = TRUE
            {_DEDUP_VILLA}
        ),
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
        )
        SELECT
            v.nombre, v.tipovilla_nombre_comercial, v.tipovilla_descripcion,
            v.capacidad_pax, v.capacidad_camas, v.numero_banos, v.numero_plantas,
            v.m2_habitables, v.m2_parcela,
            v.tiene_piscina_privada, v.tiene_piscina_comun, v.piscina_climatizada,
            v.tiene_jardin, v.tiene_garaje, v.admite_animales,
            f.tiene_internet, f.tiene_aire_acondicionado, f.tiene_lavadora, f.tiene_lavavajillas,
            v.pueblo_cercano, v.zona, v.region,
            v.rating_exterior, v.rating_interior, v.rating_vistas,
            v.es_activo, v.es_visible, v.es_recomendada
        FROM villa_dedup v
        LEFT JOIN ficha f ON SAFE_CAST(v.codigo_busqueda AS INT64) = f.propiedad_codigo
        ORDER BY v.nombre
    """
    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_BILLING_CAP),
        ).result())
    except Exception as e:
        log.exception("listar_propiedades: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}
    matches = [_row_to_dict(r) for r in rows]
    return {"matches": matches, "count": len(matches)}


def buscar_propiedades(
    ubicacion: str | None = None,
    zona: str | None = None,
    capacidad_min: int | None = None,
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
        camas_min: Número mínimo de camas.
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
        Diccionario con 'matches' (lista de propiedades, máx. 20) y 'count'.
    """
    conditions: list[str] = ["v.es_activo = TRUE", "v.es_visible = TRUE"]
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

    if camas_min is not None:
        conditions.append("v.capacidad_camas >= @camas_min")
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
        conditions.append(f"f.tiene_internet = {'TRUE' if internet else 'FALSE'}")

    if aire_acondicionado is not None:
        conditions.append(f"f.tiene_aire_acondicionado = {'TRUE' if aire_acondicionado else 'FALSE'}")

    if lavadora is not None:
        conditions.append(f"f.tiene_lavadora = {'TRUE' if lavadora else 'FALSE'}")

    if lavavajillas is not None:
        conditions.append(f"f.tiene_lavavajillas = {'TRUE' if lavavajillas else 'FALSE'}")

    if texto:
        conditions.append(
            "(LOWER(v.nombre) LIKE LOWER(@texto) OR LOWER(v.tipovilla_descripcion) LIKE LOWER(@texto))"
        )
        params.append(bigquery.ScalarQueryParameter("texto", "STRING", f"%{texto.strip()}%"))

    where = f"WHERE {' AND '.join(conditions)}"
    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            {_DEDUP_VILLA}
        ),
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
        )
        SELECT
            v.nombre, v.tipovilla_nombre_comercial, v.tipovilla_descripcion,
            v.capacidad_pax, v.capacidad_camas, v.numero_banos,
            v.m2_habitables,
            v.tiene_piscina_privada, v.tiene_jardin, v.tiene_garaje, v.admite_animales,
            f.tiene_internet, f.tiene_aire_acondicionado, f.tiene_lavadora, f.tiene_lavavajillas,
            v.pueblo_cercano, v.zona, v.region,
            v.rating_exterior, v.rating_interior, v.rating_vistas
        FROM villa_dedup v
        LEFT JOIN ficha f ON SAFE_CAST(v.codigo_busqueda AS INT64) = f.propiedad_codigo
        {where}
        ORDER BY v.nombre
        LIMIT 20
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
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [_row_to_dict(r) for r in rows]
    return {"matches": matches, "count": len(matches)}


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

    Args:
        rating_min: Puntuación media mínima (escala 1-6). Si no se especifica, ordena por rating desc.
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira…).
        capacidad_min: Número mínimo de personas.
        piscina: True para exigir piscina privada.

    Returns:
        Diccionario con 'matches' (lista de propiedades, máx. 20) y 'count'.
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
                capacidad_pax, capacidad_camas, numero_banos,
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
        SELECT * FROM base
        {where}
        ORDER BY rating_medio DESC
        LIMIT 20
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
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [_row_to_dict(r) for r in rows]
    return {"matches": matches, "count": len(matches)}


def obtener_detalle_propiedad(nombre: str) -> dict[str, Any]:
    """Devuelve la ficha completa de una propiedad específica.

    Usa cuando el usuario pregunte por una villa concreta por su nombre,
    quiera más información sobre una propiedad, o pida ver todos los detalles.
    Incluye dirección completa, coordenadas, métricas de habitaciones, ratings,
    amenidades (internet, aire acondicionado, lavadora, lavavajillas) y el
    desglose real de los baños (cuántos tienen bañera, ducha, jacuzzi, bidé
    o son en-suite).

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
            v.capacidad_pax, v.capacidad_camas, v.numero_banos, v.numero_plantas,
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

    # El desglose de baños es enriquecimiento opcional. Se consulta por separado
    # para que un cambio o ausencia en Planta/Banio no inutilice la ficha entera.
    villa_ids = [match["villa_id"] for match in matches]
    query_banios = f"""
        SELECT
            p.villa_id,
            COUNT(b.banio_id) AS banios_con_detalle,
            COUNTIF(b.tiene_baniera OR b.tiene_baniera_suelta) AS banios_con_banera,
            COUNTIF(
                b.tiene_ducha OR b.tiene_ducha_plato OR b.tiene_ducha_obra
                OR b.tiene_ducha_hidromasaje
            ) AS banios_con_ducha,
            COUNTIF(b.tiene_jacuzzi OR b.tiene_jacuzzi_baniera) AS banios_con_jacuzzi,
            COUNTIF(b.tiene_bide) AS banios_con_bide,
            COUNTIF(b.es_en_suite) AS banios_ensuite
        FROM {TABLA_PLANTA} p
        JOIN {TABLA_BANIO} b ON b.planta_id = p.planta_id AND b.es_activo = TRUE
        WHERE p.villa_id IN UNNEST(@villa_ids)
        GROUP BY p.villa_id
    """
    banios_por_villa: dict[str, dict[str, Any]] = {}
    detalle_banios_disponible = True
    try:
        rows_banios = list(_bq.query(
            query_banios,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("villa_ids", "STRING", villa_ids)
                ],
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
        banios_por_villa = {
            row["villa_id"]: _row_to_dict(row)
            for row in rows_banios
        }
    except Exception:
        detalle_banios_disponible = False
        log.warning(
            "obtener_detalle_propiedad: desglose de baños no disponible",
            exc_info=True,
        )

    for match in matches:
        villa_id = match.pop("villa_id")
        codigo_raw = match.pop("codigo_busqueda", None)
        codigo = int(codigo_raw) if str(codigo_raw or "").isdigit() else None
        detalle_ficha = ficha_por_codigo.get(codigo, {}) if codigo else {}
        detalle_ficha.pop("propiedad_codigo", None)
        match.update(detalle_ficha)
        detalle_banios = banios_por_villa.get(villa_id, {})
        detalle_banios.pop("villa_id", None)
        match.update(detalle_banios)

    return {
        "matches": matches,
        "count": len(matches),
        "detalle_amenidades_disponible": detalle_amenidades_disponible,
        "detalle_banios_disponible": detalle_banios_disponible,
    }


_DOMINIO_WEB = "abahanavillas.com"


async def _fetch_con_playwright(url: str) -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="es-ES")
        await page.goto(url, wait_until="load", timeout=30000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3000)
        html = await page.content()
        await browser.close()
    return html


def _playwright_en_hilo(url: str) -> str:
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
    parsed = urlparse(url)
    if _DOMINIO_WEB not in parsed.netloc:
        return {"error": f"Solo se permiten URLs de {_DOMINIO_WEB}"}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            html = pool.submit(_playwright_en_hilo, url).result(timeout=45)
    except Exception as e:
        log.exception("consultar_web: error al obtener %s", url)
        return {"error": str(e), "url": url}

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

    return {
        "titulo": titulo,
        "contenido": contenido[:9000],
        "enlaces": seccion_enlaces,
        "url": url,
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

    conditions = ["v.es_activo = TRUE", "v.es_visible = TRUE"]
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
    if piscina is not None:
        conditions.append(
            f"v.tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}"
        )

    limite = min(max(1, limite), 50)
    where = " AND ".join(conditions)
    query = f"""
        WITH villa_dedup AS (
            SELECT *
            FROM {TABLA_VILLA}
            {_DEDUP_VILLA}
        ),
        disponibles AS (
            SELECT
                v.nombre,
                v.pueblo_cercano,
                v.zona,
                v.region,
                v.capacidad_pax,
                v.capacidad_camas,
                v.numero_banos,
                v.tiene_piscina_privada,
                v.admite_animales
            FROM villa_dedup v
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
        estado_codigo = estado_reserva.strip().upper()
        estado_nombre = _ESTADOS_RESERVA.get(estado_codigo, estado_codigo)
        conditions.append(
            "UPPER(r.estado_reserva) IN (@estado_reserva, @estado_reserva_nombre)"
        )
        params.extend([
            bigquery.ScalarQueryParameter(
                "estado_reserva", "STRING", estado_codigo
            ),
            bigquery.ScalarQueryParameter(
                "estado_reserva_nombre", "STRING", estado_nombre
            ),
        ])

    if estado_documento:
        documento_codigo = estado_documento.strip().upper()
        documento_nombre = _ESTADOS_DOCUMENTO.get(
            documento_codigo, documento_codigo
        )
        conditions.append(
            "UPPER(r.estado_documento) IN "
            "(@estado_documento, @estado_documento_nombre)"
        )
        params.extend([
            bigquery.ScalarQueryParameter(
                "estado_documento", "STRING", documento_codigo
            ),
            bigquery.ScalarQueryParameter(
                "estado_documento_nombre", "STRING", documento_nombre
            ),
        ])

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
    - Solo se permiten SELECT/WITH (nada de INSERT/UPDATE/DELETE/DDL).
    - El resultado se limita a 50 filas.

    Args:
        query: sentencia SQL SELECT/WITH completa.

    Returns:
        Diccionario con 'rows' (máx. 50), 'count' y, si aplica, 'error'.
    """
    normalized = query.strip().upper()
    if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
        return {"rows": [], "count": 0, "error": "Solo se permiten consultas SELECT o WITH."}

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
- Usa `consultar_web(url)` cuando el usuario pregunte por información de la empresa:
  política de privacidad, aviso legal, condiciones, contacto, destinos, etc.
- Infiere la URL según el contexto (ej. política de privacidad →
  https://www.abahanavillas.com/es/politica-de-privacidad/).
- Si la primera URL falla o no tiene contenido relevante, prueba variaciones.

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
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad, camas,
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
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad, camas,
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
- `buscar_propiedades(...)`: búsqueda con filtros: ubicación, zona, capacidad, camas,
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
    tools=[
        obtener_fecha_hora_actual,
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        consultar_disponibilidad,
        consultar_web,
        buscar_internet,
        consultar_feedback_negativo,
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
        consultar_web,
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
        consultar_web,
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
