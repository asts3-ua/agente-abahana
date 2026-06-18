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
import logging
import os
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from google.adk.agents import Agent
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.cloud import bigquery

log = logging.getLogger("agente-villas")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
DATASET = "silver_clean"
TABLA_PROPIEDADES = f"`{PROJECT_ID}.{DATASET}.dim_propiedades`"
TABLA_BOOKINGS = f"`{PROJECT_ID}.{DATASET}.int_etendo_bookings`"

_bq = bigquery.Client(project=PROJECT_ID)
_BILLING_CAP = 10 * 1024 * 1024  # 10 MB por consulta


# ---------------------------------------------------------------------------
# Herramientas
# ---------------------------------------------------------------------------

def listar_propiedades() -> dict[str, Any]:
    """Devuelve todas las propiedades disponibles con sus características.

    Usa cuando el usuario pida ver el catálogo completo o todas las villas
    sin especificar filtros.
    """
    query = f"SELECT * FROM {TABLA_PROPIEDADES} ORDER BY nombre"
    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_BILLING_CAP),
        ).result())
    except Exception as e:
        log.exception("listar_propiedades: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}
    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


def buscar_propiedades(
    ubicacion: str | None = None,
    zona: str | None = None,
    capacidad_min: int | None = None,
    camas_min: int | None = None,
    banos_min: int | None = None,
    metros_habitables_min: int | None = None,
    piscina: bool | None = None,
    internet: bool | None = None,
    aire_acondicionado: bool | None = None,
    lavadora: bool | None = None,
    lavavajillas: bool | None = None,
    admite_animales: bool | None = None,
    texto: str | None = None,
) -> dict[str, Any]:
    """Busca propiedades aplicando cualquier combinación de filtros.

    Usa esta función para todos los criterios de búsqueda: ubicación,
    capacidad, camas, baños, metros, y cualquier amenidad (piscina,
    internet, aire acondicionado, lavadora, lavavajillas, mascotas).

    Args:
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira, Benidorm, Dénia…).
        zona: Nombre de zona geográfica (Costa Blanca Norte, Sur…).
        capacidad_min: Número mínimo de personas que debe admitir.
        camas_min: Número mínimo de camas.
        banos_min: Número mínimo de baños.
        metros_habitables_min: Metros habitables mínimos.
        piscina: True para exigir piscina privada.
        internet: True para exigir internet.
        aire_acondicionado: True para exigir aire acondicionado en salón.
        lavadora: True para exigir lavadora.
        lavavajillas: True para exigir lavavajillas.
        admite_animales: True para propiedades que admiten mascotas.
        texto: Busca en nombre y tipo de villa.

    Returns:
        Diccionario con 'matches' (lista de propiedades, máx. 20) y 'count'.
    """
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if ubicacion:
        conditions.append("LOWER(pueblo_cercano) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter("ubicacion", "STRING", f"%{ubicacion.strip()}%"))

    if zona:
        conditions.append("LOWER(zona_nombre) LIKE LOWER(@zona)")
        params.append(bigquery.ScalarQueryParameter("zona", "STRING", f"%{zona.strip()}%"))

    if capacidad_min is not None:
        conditions.append("capacidad_pax >= @capacidad_min")
        params.append(bigquery.ScalarQueryParameter("capacidad_min", "INT64", capacidad_min))

    if camas_min is not None:
        conditions.append("total_camas >= @camas_min")
        params.append(bigquery.ScalarQueryParameter("camas_min", "INT64", camas_min))

    if banos_min is not None:
        conditions.append("total_banos >= @banos_min")
        params.append(bigquery.ScalarQueryParameter("banos_min", "INT64", banos_min))

    if metros_habitables_min is not None:
        conditions.append("metros_habitables >= @metros_habitables_min")
        params.append(bigquery.ScalarQueryParameter("metros_habitables_min", "FLOAT64", float(metros_habitables_min)))

    if piscina is not None:
        conditions.append(f"tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")

    if internet is not None:
        conditions.append(f"tiene_internet = {'TRUE' if internet else 'FALSE'}")

    if aire_acondicionado is not None:
        conditions.append(f"tiene_aire_salon = {'TRUE' if aire_acondicionado else 'FALSE'}")

    if lavadora is not None:
        conditions.append(f"tiene_lavadora = {'TRUE' if lavadora else 'FALSE'}")

    if lavavajillas is not None:
        conditions.append(f"tiene_lavavajillas = {'TRUE' if lavavajillas else 'FALSE'}")

    if admite_animales is not None:
        conditions.append(f"admite_animales = {'TRUE' if admite_animales else 'FALSE'}")

    if texto:
        conditions.append(
            "(LOWER(propiedad_nombre) LIKE LOWER(@texto) OR LOWER(tipo_villa_descripcion) LIKE LOWER(@texto))"
        )
        params.append(bigquery.ScalarQueryParameter("texto", "STRING", f"%{texto.strip()}%"))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT DISTINCT
            propiedad_nombre, tipo_villa_descripcion,
            capacidad_pax, total_camas, total_banos, metros_habitables,
            tiene_piscina_privada, tiene_internet, tiene_aire_salon,
            tiene_lavadora, tiene_lavavajillas, admite_animales,
            pueblo_cercano, zona_nombre, region_nombre
        FROM {TABLA_BOOKINGS}
        {where}
        ORDER BY propiedad_nombre
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

    matches = [dict(r.items()) for r in rows]
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
            SELECT DISTINCT
                propiedad_nombre, pueblo_cercano, zona_nombre, region_nombre,
                capacidad_pax, total_camas, total_banos,
                tiene_piscina_privada, tiene_internet, admite_animales,
                score_rating_banos, score_rating_cocina,
                score_rating_interior, score_rating_exterior,
                ROUND(
                    (COALESCE(score_rating_banos, 0) + COALESCE(score_rating_cocina, 0) +
                     COALESCE(score_rating_interior, 0) + COALESCE(score_rating_exterior, 0)) /
                    NULLIF(
                        (CASE WHEN score_rating_banos IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN score_rating_cocina IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN score_rating_interior IS NOT NULL THEN 1 ELSE 0 END +
                         CASE WHEN score_rating_exterior IS NOT NULL THEN 1 ELSE 0 END), 0
                    ), 2
                ) AS rating_medio
            FROM {TABLA_BOOKINGS}
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

    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


def obtener_detalle_propiedad(nombre: str) -> dict[str, Any]:
    """Devuelve la ficha completa de una propiedad específica.

    Usa cuando el usuario pregunte por una villa concreta por su nombre,
    quiera más información sobre una propiedad, o pida ver todos los detalles.
    Incluye dirección completa, coordenadas, métricas de habitaciones y ratings.

    Args:
        nombre: Nombre o parte del nombre de la propiedad.

    Returns:
        Diccionario con 'matches' (lista de propiedades encontradas) y 'count'.
    """
    params = [bigquery.ScalarQueryParameter("nombre", "STRING", f"%{nombre.strip()}%")]
    query = f"""
        SELECT DISTINCT
            propiedad_nombre, licencia_turismo, tipo_villa_descripcion,
            anyo_construccion, metros_parcela, metros_habitables,
            capacidad_pax, total_camas, camas_dobles, camas_individuales,
            total_banos, total_plantas,
            tiene_internet, tiene_aire_salon, tiene_lavadora,
            tiene_lavavajillas, tiene_piscina_privada, admite_animales,
            pueblo_cercano, zona_nombre, region_nombre, direccion_completa,
            latitud, longitud,
            score_rating_banos, score_rating_cocina,
            score_rating_interior, score_rating_exterior
        FROM {TABLA_BOOKINGS}
        WHERE LOWER(propiedad_nombre) LIKE LOWER(@nombre)
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

    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


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


# ---------------------------------------------------------------------------
# Instrucciones por rol
# ---------------------------------------------------------------------------

_INSTRUCCION_BASE = """
Eres el asistente virtual de Abahana Villas, empresa de alquiler de villas vacacionales
en la Costa Blanca (España).

## Reglas generales
- Responde SIEMPRE en español.
- Para ubicación usa pueblo_cercano (Altea, Calpe, Moraira…) o zona_nombre.
- Muestra el rating_medio cuando uses buscar_por_valoracion.
- Puedes combinar varios filtros en una sola llamada.
- Nunca inventes datos. Si no hay resultados, díselo y sugiere alternativas.
- Muestra los datos de forma clara: nombre, ubicación, capacidad, amenidades.
- No tenemos información de precios por noche en el sistema actual.

## Web corporativa
- URL base: https://www.abahanavillas.com/es/
- Usa `consultar_web(url)` cuando el usuario pregunte por información de la empresa:
  política de privacidad, aviso legal, condiciones, contacto, destinos, etc.
- Infiere la URL según el contexto (ej. política de privacidad →
  https://www.abahanavillas.com/es/politica-de-privacidad/).
- Si la primera URL falla o no tiene contenido relevante, prueba variaciones.

## Búsqueda en internet
- Usa `agente_busqueda_internet` para información externa no disponible en BigQuery
  ni en abahanavillas.com: fiestas locales, eventos, clima, atracciones turísticas,
  horarios de mercados, datos de pueblos (Calpe, Altea, Moraira, Dénia…).
- No inventes fechas ni eventos; si no hay datos internos, busca en internet.
- Para preguntas mixtas (ej. "villas en Calpe y cuándo son las fiestas"), combina
  herramientas de BigQuery con búsqueda en internet.
- Cita las fuentes cuando uses información obtenida de internet.
""".strip()

INSTRUCTION_CLIENTE = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con cualquier combinación de filtros (ubicación,
  capacidad, camas, baños, metros, piscina, internet, aire acondicionado, lavadora,
  lavavajillas, mascotas).
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `agente_busqueda_internet`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…).
""".strip()

INSTRUCTION_INTERNO = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con cualquier combinación de filtros (ubicación,
  capacidad, camas, baños, metros, piscina, internet, aire acondicionado, lavadora,
  lavavajillas, mascotas).
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo.
- `obtener_detalle_propiedad(nombre)`: ficha completa con dirección, coordenadas,
  desglose de camas, metros habitables y ratings por categoría. Úsala cuando el
  usuario pregunte por una villa concreta o pida más detalles.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `agente_busqueda_internet`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…).

## Contexto de uso interno
Eres la versión para agentes de ventas y equipo interno. Puedes mostrar la dirección
completa, coordenadas y todos los datos de la ficha de propiedad.
""".strip()

INSTRUCTION_ADMIN = f"""{_INSTRUCCION_BASE}

## Herramientas disponibles
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda con cualquier combinación de filtros (ubicación,
  capacidad, camas, baños, metros, piscina, internet, aire acondicionado, lavadora,
  lavavajillas, mascotas).
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas o con
  un rating mínimo.
- `obtener_detalle_propiedad(nombre)`: ficha completa con dirección, coordenadas,
  desglose de camas, metros habitables y ratings por categoría.
- `consultar_web(url)`: información corporativa de la web (política de privacidad,
  aviso legal, condiciones, contacto, destinos…).
- `agente_busqueda_internet`: búsqueda en internet (fiestas, eventos, clima,
  atracciones, horarios de pueblos de la Costa Blanca…).

## Contexto de uso
Eres la versión de administración. Tienes acceso completo a todos los datos disponibles.
""".strip()


# ---------------------------------------------------------------------------
# Sub-agente de búsqueda en internet (patrón Agent-as-Tool)
# ---------------------------------------------------------------------------

agente_busqueda_internet = Agent(
    name="agente_busqueda_internet",
    model="gemini-2.5-flash",
    description=(
        "Busca información actual en internet: fiestas locales, eventos, "
        "clima, atracciones turísticas, horarios y datos de pueblos de la Costa Blanca."
    ),
    instruction="""
Eres un especialista en búsqueda web para Abahana Villas.
- Responde SIEMPRE en español.
- Busca información actual y verificable (fechas de fiestas, eventos, clima, etc.).
- Prioriza fuentes oficiales: ayuntamientos, turismo, medios locales.
- Incluye fechas concretas cuando existan.
- Cita las fuentes al final de tu respuesta.
- Si no encuentras información fiable, dilo claramente.
""".strip(),
    tools=[google_search],
)

herramienta_busqueda_internet = AgentTool(
    agent=agente_busqueda_internet,
    propagate_grounding_metadata=True,
)


# ---------------------------------------------------------------------------
# Agentes por rol
# ---------------------------------------------------------------------------

agent_cliente = Agent(
    name="abahana_villas_agent_cliente",
    model="gemini-2.5-flash",
    description="Asistente público de villas Abahana (rol: cliente)",
    instruction=INSTRUCTION_CLIENTE,
    tools=[
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        consultar_web,
        herramienta_busqueda_internet,
    ],
)

agent_interno = Agent(
    name="abahana_villas_agent_interno",
    model="gemini-2.5-flash",
    description="Asistente interno de villas Abahana (rol: interno)",
    instruction=INSTRUCTION_INTERNO,
    tools=[
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
        consultar_web,
        herramienta_busqueda_internet,
    ],
)

agent_admin = Agent(
    name="abahana_villas_agent_admin",
    model="gemini-2.5-flash",
    description="Asistente de administración de villas Abahana (rol: admin)",
    instruction=INSTRUCTION_ADMIN,
    tools=[
        listar_propiedades,
        buscar_propiedades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
        consultar_web,
        herramienta_busqueda_internet,
    ],
)

AGENTS: dict[str, Agent] = {
    "cliente": agent_cliente,
    "interno": agent_interno,
    "admin": agent_admin,
}

# Compatibilidad con `adk web` (espera root_agent en agente_villas/__init__.py)
root_agent = agent_interno
