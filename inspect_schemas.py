"""Dump schemas and duplicate stats for Dataform ETL targets."""
from __future__ import annotations

from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 100 * 1024 * 1024
SILVER = f"{PROJECT}.silver_clean"
BRONZE = f"{PROJECT}.bronze_raw"

TABLES = [
    "stg_etendo_Proveedor",
    "stg_etendo_Reserva",
    "stg_etendo_Tarifa",
    "stg_etendo_Tercero",
    "stg_etendo_TipoEstancia",
    "stg_etendo_TipoProducto",
    "stg_etendo_TipoTarifa",
    "stg_etendo_TipoUnidad",
    "stg_etendo_Villa",
    "stg_etendo_OBRE_Resource",
]

BRONZE_SOURCES = [
    "etendo_BusinessPartner",
    "etendo_Order",
    "etendo_PricingPriceList",
    "etendo_ProductCategory",
    "etendo_UOM",
    "etendo_OBRE_Resource",
]


def q(client, sql):
    return [
        dict(r)
        for r in client.query(
            sql,
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP),
            location=LOCATION,
        ).result()
    ]


def main():
    client = bigquery.Client(project=PROJECT, location=LOCATION)

    print("=== SILVER SCHEMAS ===")
    for table in TABLES:
        t = client.get_table(f"{SILVER}.{table}")
        cols = [f"{f.name}:{f.field_type}" for f in t.schema]
        print(f"\n{table} ({t.num_rows} rows)")
        print(", ".join(cols))

    print("\n=== BRONZE DUPLICATE CHECK (by payload.id) ===")
    for table in BRONZE_SOURCES:
        rows = q(
            client,
            f"""
            SELECT
              COUNT(*) AS rows,
              COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids,
              COUNT(*) - COUNT(DISTINCT LAX_STRING(payload.id)) AS dup_extra
            FROM `{BRONZE}.{table}`
            """,
        )
        print(table, rows[0])

    print("\n=== BRONZE ZEBRA ingestion versions ===")
    print(
        q(
            client,
            f"""
            SELECT ingestion_dt, run_id, COUNT(*) AS n
            FROM `{BRONZE}.etendo_OBRE_Resource`
            WHERE LAX_STRING(payload.id) = '5BD6BBB7CF0B415C89A3F23B76392F2B'
            GROUP BY 1,2
            ORDER BY ingestion_dt DESC
            """,
        )
    )

    print("\n=== Proveedor duplicate sample ===")
    print(
        q(
            client,
            f"""
            SELECT nif, COUNT(*) AS n, ARRAY_AGG(email LIMIT 3) AS emails
            FROM `{SILVER}.stg_etendo_Proveedor`
            GROUP BY nif
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 5
            """,
        )
    )

    print("\n=== Tercero duplicate sample ===")
    print(
        q(
            client,
            f"""
            SELECT nif, COUNT(*) AS n
            FROM `{SILVER}.stg_etendo_Tercero`
            GROUP BY nif
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 5
            """,
        )
    )

    print("\n=== Tarifa duplicate sample ===")
    print(
        q(
            client,
            f"""
            SELECT tarifa_id, COUNT(*) AS n
            FROM `{SILVER}.stg_etendo_Tarifa`
            GROUP BY tarifa_id
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 5
            """,
        )
    )

    print("\n=== Reserva: villa_id format sample orphan vs matched ===")
    print(
        q(
            client,
            f"""
            SELECT
              CASE WHEN v.villa_id IS NULL THEN 'orphan' ELSE 'matched' END AS bucket,
              SUBSTR(r.villa_id, 1, 8) AS villa_prefix,
              COUNT(*) AS n
            FROM `{SILVER}.stg_etendo_Reserva` r
            LEFT JOIN `{SILVER}.stg_etendo_Villa` v ON r.villa_id = v.villa_id
            WHERE r.villa_id IS NOT NULL
            GROUP BY 1,2
            ORDER BY n DESC
            LIMIT 10
            """,
        )
    )


if __name__ == "__main__":
    main()
