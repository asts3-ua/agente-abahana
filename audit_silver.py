"""
Auditoría de calidad de datos en silver_clean (solo lectura).
Ejecutar: python audit_silver.py
Requiere: credenciales ADC activas (gcloud auth application-default login)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from google.cloud import bigquery

PROJECT = "abahanaweb"
DATASET = "silver_clean"
LOCATION = "EU"
BILLING_CAP = 100 * 1024 * 1024  # 100 MB

VILLA = f"`{PROJECT}.{DATASET}.stg_etendo_Villa`"
RESERVA = f"`{PROJECT}.{DATASET}.stg_etendo_Reserva`"


def run_query(client: bigquery.Client, sql: str, label: str) -> list[dict]:
    print(f"\n{'='*60}\n{label}\n{'='*60}")
    try:
        rows = list(
            client.query(
                sql,
                job_config=bigquery.QueryJobConfig(maximum_bytes_billed=BILLING_CAP),
                location=LOCATION,
            ).result()
        )
        out = [dict(r) for r in rows]
        if not out:
            print("(sin filas — OK o tabla vacía)")
        else:
            for row in out[:30]:
                print(row)
            if len(out) > 30:
                print(f"... y {len(out) - 30} filas más")
        return out
    except Exception as exc:
        print(f"ERROR: {exc}")
        return []


def main() -> int:
    print(f"Auditoría silver_clean — {datetime.now(timezone.utc).isoformat()}")
    try:
        client = bigquery.Client(project=PROJECT, location=LOCATION)
        tables = list(client.list_tables(DATASET))
        print(f"\nTablas en {DATASET}: {len(tables)}")
        for t in sorted(tables, key=lambda x: x.table_id):
            print(f"  - {t.table_id}")
    except Exception as exc:
        print(f"\nNo se pudo conectar a BigQuery: {exc}")
        print("\nRenueva credenciales con:")
        print("  gcloud auth application-default login")
        return 1

    # Inventario
    run_query(
        client,
        f"""
        SELECT table_id, row_count, ROUND(size_bytes/1024/1024, 2) AS size_mb
        FROM `{PROJECT}.{DATASET}.__TABLES__`
        ORDER BY row_count DESC
        """,
        "1. Inventario del dataset",
    )

    # Esquemas clave
    for table_id in ("stg_etendo_Villa", "stg_etendo_Reserva", "dim_propiedades", "int_etendo_bookings"):
        try:
            table = client.get_table(f"{PROJECT}.{DATASET}.{table_id}")
            cols = [f"{f.name} ({f.field_type})" for f in table.schema]
            print(f"\n--- Esquema {table_id} ({len(cols)} cols) ---")
            print(", ".join(cols[:25]))
            if len(cols) > 25:
                print(f"... +{len(cols)-25} columnas más")
        except Exception:
            print(f"\n--- {table_id}: no existe o sin acceso ---")

    # Villa: conteos básicos
    run_query(
        client,
        f"""
        SELECT
          COUNT(*) AS total_filas,
          COUNT(DISTINCT villa_id) AS villas_unicas,
          COUNTIF(es_activo = TRUE) AS activas,
          COUNTIF(es_visible = TRUE) AS visibles,
          COUNTIF(es_activo = TRUE AND es_visible = TRUE) AS activas_y_visibles
        FROM {VILLA}
        """,
        "2. stg_etendo_Villa — conteos",
    )

    # Villa: duplicados por villa_id
    run_query(
        client,
        f"""
        SELECT villa_id, COUNT(*) AS n, ARRAY_AGG(nombre LIMIT 3) AS nombres
        FROM {VILLA}
        GROUP BY villa_id
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 20
        """,
        "3. stg_etendo_Villa — duplicados por villa_id",
    )

    # Villa: duplicados por nombre
    run_query(
        client,
        f"""
        SELECT nombre, COUNT(*) AS n, ARRAY_AGG(CAST(villa_id AS STRING) LIMIT 3) AS villa_ids
        FROM {VILLA}
        GROUP BY nombre
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 20
        """,
        "4. stg_etendo_Villa — duplicados por nombre",
    )

    # Villa: nulos en campos clave
    run_query(
        client,
        f"""
        SELECT
          COUNTIF(villa_id IS NULL) AS villa_id_null,
          COUNTIF(nombre IS NULL OR TRIM(nombre) = '') AS nombre_vacio,
          COUNTIF(pueblo_cercano IS NULL) AS pueblo_null,
          COUNTIF(es_activo IS NULL) AS es_activo_null,
          COUNTIF(es_visible IS NULL) AS es_visible_null,
          COUNTIF(capacidad_pax IS NULL) AS capacidad_null
        FROM {VILLA}
        """,
        "5. stg_etendo_Villa — nulos en campos clave",
    )

    # Villa: ratings fuera de rango
    run_query(
        client,
        f"""
        SELECT
          COUNTIF(rating_exterior < 1 OR rating_exterior > 6) AS rating_exterior_fuera,
          COUNTIF(rating_interior < 1 OR rating_interior > 6) AS rating_interior_fuera,
          COUNTIF(rating_vistas < 1 OR rating_vistas > 6) AS rating_vistas_fuera
        FROM {VILLA}
        """,
        "6. stg_etendo_Villa — ratings fuera de rango 1-6",
    )

    # Villa: activo pero no visible (inconsistencia)
    run_query(
        client,
        f"""
        SELECT es_activo, es_visible, COUNT(*) AS n
        FROM {VILLA}
        GROUP BY es_activo, es_visible
        ORDER BY n DESC
        """,
        "7. stg_etendo_Villa — cruce es_activo × es_visible",
    )

    # Reserva: conteos
    run_query(
        client,
        f"""
        SELECT
          COUNT(*) AS total_filas,
          COUNT(DISTINCT localizador) AS localizadores_unicos,
          COUNTIF(fecha_entrada IS NULL) AS sin_fecha_entrada,
          COUNTIF(fecha_salida IS NULL) AS sin_fecha_salida,
          COUNTIF(importe_total IS NULL) AS sin_importe,
          COUNTIF(villa_id IS NULL) AS sin_villa_id
        FROM {RESERVA}
        """,
        "8. stg_etendo_Reserva — conteos",
    )

    # Reserva: duplicados por localizador
    run_query(
        client,
        f"""
        SELECT localizador, COUNT(*) AS n
        FROM {RESERVA}
        GROUP BY localizador
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 20
        """,
        "9. stg_etendo_Reserva — duplicados por localizador",
    )

    # Join huérfanos
    run_query(
        client,
        f"""
        SELECT COUNT(*) AS reservas_sin_villa
        FROM {RESERVA} r
        LEFT JOIN {VILLA} v ON r.villa_id = v.villa_id
        WHERE r.villa_id IS NOT NULL AND v.villa_id IS NULL
        """,
        "10. Reservas con villa_id huérfano (sin match en Villa)",
    )

    # Estados de reserva
    run_query(
        client,
        f"""
        SELECT estado_reserva, estado_documento, COUNT(*) AS n
        FROM {RESERVA}
        GROUP BY estado_reserva, estado_documento
        ORDER BY n DESC
        LIMIT 20
        """,
        "11. stg_etendo_Reserva — distribución de estados",
    )

    # Fechas inconsistentes
    run_query(
        client,
        f"""
        SELECT COUNT(*) AS salida_antes_entrada
        FROM {RESERVA}
        WHERE fecha_entrada IS NOT NULL AND fecha_salida IS NOT NULL
          AND fecha_salida < fecha_entrada
        """,
        "12. Reservas con fecha_salida < fecha_entrada",
    )

    # Comparar tablas doc vs código si existen
    for alt in ("dim_propiedades", "int_etendo_bookings"):
        run_query(
            client,
            f"""
            SELECT COUNT(*) AS filas
            FROM `{PROJECT}.{DATASET}.{alt}`
            """,
            f"13. {alt} — conteo (si existe)",
        )

    print("\n\nAuditoría completada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
