"""
Agente de Abahana Villas — responde preguntas en lenguaje natural sobre
villas de alquiler vacacional usando Google ADK + BigQuery.
"""

import logging
import os
from typing import Any

from google.adk.agents import Agent
from google.cloud import bigquery

log = logging.getLogger("agente-villas")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
DATASET = "abahana"
TABLA = f"`{PROJECT_ID}.{DATASET}.villas`"

_bq = bigquery.Client(project=PROJECT_ID)

_BILLING_CAP = 10 * 1024 * 1024  # 10 MB por consulta


def listar_villas() -> dict[str, Any]:
    """Devuelve todas las villas disponibles con todos sus campos.

    Usa esta función cuando el usuario pida ver el catálogo completo,
    todas las villas disponibles, o no especifique ningún filtro concreto.
    """
    query = f"SELECT * FROM {TABLA} ORDER BY precio_noche"
    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=_BILLING_CAP),
        ).result())
    except Exception as e:
        log.exception("listar_villas: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


def buscar_villas(
    texto: str | None = None,
    ubicacion: str | None = None,
    capacidad_min: int | None = None,
    habitaciones_min: int | None = None,
    precio_max: float | None = None,
    precio_min: float | None = None,
    piscina: bool | None = None,
    aire_acondicionado: bool | None = None,
) -> dict[str, Any]:
    """Busca villas aplicando uno o varios filtros combinados.

    Usa esta función cuando el usuario especifique cualquier criterio de búsqueda.
    Todos los parámetros son opcionales — usa solo los que el usuario mencione.

    Args:
        texto: Busca en nombre y descripción. Sirve para cualquier característica
               no estructurada: "terraza", "jacuzzi", "vistas al mar", "playa",
               nombre de villa, etc.
        ubicacion: Localidad (Altea, Calpe, Moraira, Benidorm, Dénia…).
                   Búsqueda parcial sin distinguir mayúsculas.
        capacidad_min: Número mínimo de personas que debe admitir la villa.
        habitaciones_min: Número mínimo de habitaciones.
        precio_max: Precio máximo por noche en euros.
        precio_min: Precio mínimo por noche en euros.
        piscina: True para exigir piscina privada.
        aire_acondicionado: True para exigir aire acondicionado.

    Returns:
        Diccionario con 'matches' (lista de villas, máx. 20) y 'count'.
    """
    conditions: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []

    if texto:
        conditions.append(
            "(LOWER(nombre) LIKE LOWER(@texto) OR LOWER(descripcion) LIKE LOWER(@texto))"
        )
        params.append(bigquery.ScalarQueryParameter("texto", "STRING", f"%{texto.strip()}%"))

    if ubicacion:
        conditions.append("LOWER(ubicacion) LIKE LOWER(@ubicacion)")
        params.append(bigquery.ScalarQueryParameter("ubicacion", "STRING", f"%{ubicacion.strip()}%"))

    if capacidad_min is not None:
        conditions.append("capacidad >= @capacidad_min")
        params.append(bigquery.ScalarQueryParameter("capacidad_min", "INT64", capacidad_min))

    if habitaciones_min is not None:
        conditions.append("habitaciones >= @habitaciones_min")
        params.append(bigquery.ScalarQueryParameter("habitaciones_min", "INT64", habitaciones_min))

    if precio_max is not None:
        conditions.append("precio_noche <= @precio_max")
        params.append(bigquery.ScalarQueryParameter("precio_max", "FLOAT64", precio_max))

    if precio_min is not None:
        conditions.append("precio_noche >= @precio_min")
        params.append(bigquery.ScalarQueryParameter("precio_min", "FLOAT64", precio_min))

    if piscina is not None:
        conditions.append(f"piscina = {'TRUE' if piscina else 'FALSE'}")

    if aire_acondicionado is not None:
        conditions.append(f"aire_acondicionado = {'TRUE' if aire_acondicionado else 'FALSE'}")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM {TABLA} {where} ORDER BY precio_noche LIMIT 20"

    try:
        rows = list(_bq.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=params,
                maximum_bytes_billed=_BILLING_CAP,
            ),
        ).result())
    except Exception as e:
        log.exception("buscar_villas: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


INSTRUCTION = """
Eres el asistente virtual de Abahana Villas, empresa de alquiler de villas vacacionales
en la Costa Blanca (España).

## Herramientas disponibles
- `listar_villas()`: catálogo completo sin filtros.
- `buscar_villas(...)`: búsqueda flexible con filtros opcionales combinables.
  Úsala con los parámetros que el usuario mencione. Para características como
  "terraza", "jacuzzi", "vistas al mar" o cualquier detalle descriptivo, usa
  el parámetro `texto`.

## Reglas
- Responde SIEMPRE en español.
- Muestra precios con € y por noche.
- Si el usuario pregunta qué villas hay, usa `listar_villas`.
- Si el usuario filtra por algo, usa `buscar_villas` con los parámetros relevantes.
- Puedes combinar varios filtros en una sola llamada.
- Nunca inventes datos. Si no hay resultados, díselo y sugiere alternativas.
""".strip()


root_agent = Agent(
    name="abahana_villas_agent",
    model="gemini-2.5-flash",
    description="Agente de villas vacacionales Abahana en la Costa Blanca",
    instruction=INSTRUCTION,
    tools=[listar_villas, buscar_villas],
)
