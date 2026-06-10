"""
Agente de Abahana Villas — responde preguntas sobre villas y reservas
usando Google ADK + BigQuery (capa silver_clean).
"""

import logging
import os
from typing import Any

from google.adk.agents import Agent
from google.cloud import bigquery

log = logging.getLogger("agente-villas")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
DATASET = "silver_clean"
TABLA_PROPIEDADES = f"`{PROJECT_ID}.{DATASET}.dim_propiedades`"
TABLA_BOOKINGS = f"`{PROJECT_ID}.{DATASET}.int_etendo_bookings`"

_bq = bigquery.Client(project=PROJECT_ID)
_BILLING_CAP = 10 * 1024 * 1024  # 10 MB por consulta


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
    piscina: bool | None = None,
    internet: bool | None = None,
    texto: str | None = None,
) -> dict[str, Any]:
    """Busca propiedades por ubicación, capacidad y amenidades básicas.

    Usa esta función para filtros comunes. Para aire acondicionado, lavadora,
    lavavajillas o mascotas usa buscar_propiedades_amenidades.

    Args:
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira, Benidorm, Dénia…).
        zona: Nombre de zona geográfica (Costa Blanca Norte, Sur…).
        capacidad_min: Número mínimo de personas que debe admitir la villa.
        camas_min: Número mínimo de camas.
        banos_min: Número mínimo de baños.
        piscina: True para exigir piscina privada.
        internet: True para exigir internet.
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

    if piscina is not None:
        conditions.append(f"tiene_piscina_privada = {'TRUE' if piscina else 'FALSE'}")

    if internet is not None:
        conditions.append(f"tiene_internet = {'TRUE' if internet else 'FALSE'}")

    if texto:
        conditions.append(
            "(LOWER(nombre) LIKE LOWER(@texto) OR LOWER(tipo_villa_descripcion) LIKE LOWER(@texto))"
        )
        params.append(bigquery.ScalarQueryParameter("texto", "STRING", f"%{texto.strip()}%"))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM {TABLA_PROPIEDADES} {where} ORDER BY nombre LIMIT 20"

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


def buscar_propiedades_amenidades(
    ubicacion: str | None = None,
    capacidad_min: int | None = None,
    piscina: bool | None = None,
    internet: bool | None = None,
    aire_acondicionado: bool | None = None,
    lavadora: bool | None = None,
    lavavajillas: bool | None = None,
    admite_animales: bool | None = None,
) -> dict[str, Any]:
    """Busca propiedades filtrando por amenidades extendidas.

    Usa esta función cuando el usuario mencione aire acondicionado, lavadora,
    lavavajillas o mascotas/animales. También sirve para combinar con ubicación
    y capacidad.

    Args:
        ubicacion: Pueblo cercano (Altea, Calpe, Moraira…).
        capacidad_min: Número mínimo de personas.
        piscina: True para exigir piscina privada.
        internet: True para exigir internet.
        aire_acondicionado: True para exigir aire acondicionado en salón.
        lavadora: True para exigir lavadora.
        lavavajillas: True para exigir lavavajillas.
        admite_animales: True para propiedades que admiten mascotas.

    Returns:
        Diccionario con 'matches' (lista de propiedades únicas, máx. 20) y 'count'.
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

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT DISTINCT
            propiedad_nombre, licencia_turismo, tipo_villa_descripcion,
            anyo_construccion, metros_parcela, metros_habitables,
            capacidad_pax, total_camas, camas_dobles, camas_individuales,
            total_banos, tiene_internet, tiene_aire_salon, tiene_lavadora,
            tiene_lavavajillas, tiene_piscina_privada, admite_animales,
            pueblo_cercano, zona_nombre, region_nombre, direccion_completa,
            latitud, longitud,
            score_rating_banos, score_rating_cocina,
            score_rating_interior, score_rating_exterior
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
        log.exception("buscar_propiedades_amenidades: error en BigQuery")
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


INSTRUCTION = """
Eres el asistente virtual de Abahana Villas, empresa de alquiler de villas vacacionales
en la Costa Blanca (España).

## Herramientas disponibles
- `listar_propiedades()`: catálogo completo sin filtros.
- `buscar_propiedades(...)`: búsqueda por ubicación, capacidad, camas, baños, piscina, internet.
- `buscar_propiedades_amenidades(...)`: cuando el usuario mencione aire acondicionado,
  lavadora, lavavajillas o mascotas/animales.
- `buscar_por_valoracion(...)`: cuando el usuario pida villas bien valoradas, con buena
  puntuación, las mejor valoradas, o especifique un rating mínimo.
- `obtener_detalle_propiedad(nombre)`: cuando el usuario pregunte por una villa concreta
  o quiera ver todos los detalles de una propiedad específica.

## Reglas
- Responde SIEMPRE en español.
- Para ubicación usa pueblo_cercano (Altea, Calpe, Moraira…) o zona_nombre.
- Muestra el rating_medio cuando uses buscar_por_valoracion.
- Puedes combinar varios filtros en una sola llamada.
- Nunca inventes datos. Si no hay resultados, díselo y sugiere alternativas.
- Muestra los datos de forma clara: nombre, ubicación, capacidad, amenidades.
- No tenemos información de precios por noche en el sistema actual.
""".strip()


root_agent = Agent(
    name="abahana_villas_agent",
    model="gemini-2.5-flash",
    description="Agente de villas vacacionales Abahana en la Costa Blanca",
    instruction=INSTRUCTION,
    tools=[
        listar_propiedades,
        buscar_propiedades,
        buscar_propiedades_amenidades,
        buscar_por_valoracion,
        obtener_detalle_propiedad,
    ],
)
