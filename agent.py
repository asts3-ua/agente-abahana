"""
Agente de Abahana Villas — responde preguntas en lenguaje natural sobre
villas de alquiler vacacional usando Google ADK + BigQuery.
"""

import logging
import os
from functools import lru_cache
from typing import Any

from google.adk.agents import Agent
from google.cloud import bigquery

log = logging.getLogger("agente-villas")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "abahanaweb")
DATASET = "abahana"
TABLA = f"`{PROJECT_ID}.{DATASET}.villas`"

_bq = bigquery.Client(project=PROJECT_ID)


def buscar_villa(nombre: str) -> dict[str, Any]:
    """Busca villas por nombre (búsqueda parcial, sin distinguir mayúsculas).

    Usa esta función cuando el usuario pregunte por una villa concreta por nombre.

    Args:
        nombre: Nombre o parte del nombre de la villa.

    Returns:
        Diccionario con 'matches' (lista de villas, máx. 5) y 'count'.
    """
    if not nombre or not nombre.strip():
        return {"matches": [], "count": 0, "error": "Nombre vacío"}

    query = f"""
        SELECT *
        FROM {TABLA}
        WHERE LOWER(nombre) LIKE LOWER(@patron)
        LIMIT 5
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("patron", "STRING", f"%{nombre.strip()}%"),
        ],
        maximum_bytes_billed=10 * 1024 * 1024,
    )
    try:
        rows = list(_bq.query(query, job_config=job_config).result())
    except Exception as e:
        log.exception("buscar_villa: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


@lru_cache(maxsize=1)
def _columnas_booleanas() -> tuple[str, ...]:
    table = _bq.get_table(f"{PROJECT_ID}.{DATASET}.villas")
    return tuple(f.name for f in table.schema if f.field_type == "BOOLEAN")


def buscar_villas_por_caracteristicas(caracteristicas: list[str]) -> dict[str, Any]:
    """Busca villas que cumplan TODAS las características booleanas indicadas.

    Usa esta función para filtrar por prestaciones: piscina, aire_acondicionado, etc.
    Pasa los nombres exactos de columnas booleanas de la tabla.

    Args:
        caracteristicas: Lista de columnas booleanas, p.ej. ["piscina", "aire_acondicionado"].

    Returns:
        Diccionario con 'matches' (máx. 10), 'count', y si hay error 'caracteristicas_disponibles'.
    """
    if not caracteristicas:
        return {"matches": [], "count": 0, "error": "Lista vacía"}

    try:
        disponibles = _columnas_booleanas()
    except Exception as e:
        return {"matches": [], "count": 0, "error": str(e)}

    desconocidas = [c for c in caracteristicas if c not in disponibles]
    if desconocidas:
        return {
            "matches": [],
            "count": 0,
            "error": f"Columnas no válidas: {desconocidas}",
            "caracteristicas_disponibles": list(disponibles),
        }

    where = " AND ".join(f"`{c}` = TRUE" for c in caracteristicas)
    query = f"SELECT * FROM {TABLA} WHERE {where} LIMIT 10"
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=10 * 1024 * 1024)
    try:
        rows = list(_bq.query(query, job_config=job_config).result())
    except Exception as e:
        log.exception("buscar_villas_por_caracteristicas: error en BigQuery")
        return {"matches": [], "count": 0, "error": str(e)}

    matches = [dict(r.items()) for r in rows]
    return {"matches": matches, "count": len(matches)}


INSTRUCTION = """
Eres el asistente virtual de Abahana Villas, empresa de alquiler de villas vacacionales
en la Costa Blanca (España).

## Herramientas disponibles
- `buscar_villa(nombre)`: para preguntas sobre una villa concreta por nombre.
- `buscar_villas_por_caracteristicas(caracteristicas)`: para filtrar por prestaciones.
  Las columnas booleanas disponibles son: `piscina`, `aire_acondicionado`.

## Reglas
- Responde SIEMPRE en español.
- Para buscar una villa por nombre, usa `buscar_villa`.
- Para filtrar por características, usa `buscar_villas_por_caracteristicas` con los
  nombres exactos de columna (p.ej. ["piscina"]).
- Muestra precios con € y por noche.
- Nunca inventes datos. Si no hay resultados, díselo y sugiere alternativas.
""".strip()


root_agent = Agent(
    name="abahana_villas_agent",
    model="gemini-2.5-flash",
    description="Agente de villas vacacionales Abahana en la Costa Blanca",
    instruction=INSTRUCTION,
    tools=[buscar_villa, buscar_villas_por_caracteristicas],
)
