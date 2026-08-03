"""Trace Villa ETL join explosion sources."""
from __future__ import annotations

from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 100 * 1024 * 1024
BRONZE = f"{PROJECT}.bronze_raw"
SILVER = f"{PROJECT}.silver_clean"
ZEBRA = "5BD6BBB7CF0B415C89A3F23B76392F2B"


def q(client, sql):
    return [dict(r) for r in client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP), location=LOCATION).result()]


def main():
    c = bigquery.Client(project=PROJECT, location=LOCATION)

    checks = [
        (
            "bronze dup by id",
            f"""
            SELECT
              COUNT(*) AS total_rows,
              COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids
            FROM `{BRONZE}.etendo_OBRE_Resource`
            """,
        ),
        (
            "bronze deduped latest per id",
            f"""
            WITH ranked AS (
              SELECT
                LAX_STRING(payload.id) AS id,
                ROW_NUMBER() OVER (
                  PARTITION BY LAX_STRING(payload.id)
                  ORDER BY ingestion_dt DESC, run_id DESC
                ) AS rn
              FROM `{BRONZE}.etendo_OBRE_Resource`
            )
            SELECT COUNTIF(rn = 1) AS rows_after_dedupe
            FROM ranked
            """,
        ),
        (
            "product rows for zebra product ref",
            f"""
            WITH v AS (
              SELECT payload
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id) = '{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (
                PARTITION BY LAX_STRING(payload.id)
                ORDER BY ingestion_dt DESC, run_id DESC
              ) = 1
            )
            SELECT
              LAX_STRING(v.payload.product) AS product_id,
              (SELECT COUNT(*) FROM `{BRONZE}.etendo_Product` p
               WHERE LAX_STRING(p.payload.id) = LAX_STRING(v.payload.product)) AS product_rows,
              (SELECT COUNT(*) FROM `{BRONZE}.etendo_opxdes_ficha_tecnica` f
               WHERE LAX_STRING(f.payload.id) = LAX_STRING(v.payload.product)) AS ficha_rows
            FROM v
            """,
        ),
        (
            "location rows joined via product",
            f"""
            WITH v AS (
              SELECT payload
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id) = '{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (
                PARTITION BY LAX_STRING(payload.id)
                ORDER BY ingestion_dt DESC, run_id DESC
              ) = 1
            ), p AS (
              SELECT payload
              FROM `{BRONZE}.etendo_Product`
              WHERE LAX_STRING(payload.id) = (SELECT LAX_STRING(payload.product) FROM v)
            )
            SELECT
              LAX_STRING(p.payload.id) AS product_id,
              LAX_STRING(p.payload.ovovLocation) AS location_id,
              (SELECT COUNT(*) FROM `{BRONZE}.etendo_Location` l
               WHERE LAX_STRING(l.payload.id) = LAX_STRING(p.payload.ovovLocation)) AS location_rows
            FROM p
            """,
        ),
        (
            "ficha tecnica rows for zebra",
            f"""
            WITH v AS (
              SELECT LAX_STRING(payload.product) AS product_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id) = '{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (
                PARTITION BY LAX_STRING(payload.id)
                ORDER BY ingestion_dt DESC, run_id DESC
              ) = 1
            )
            SELECT COUNT(*) AS ficha_n
            FROM `{BRONZE}.etendo_opxdes_ficha_tecnica` f
            JOIN v ON LAX_STRING(f.payload.product) = v.product_id
            """,
        ),
        (
            "ov tipovilla rows for zebra",
            f"""
            WITH v AS (
              SELECT LAX_STRING(payload.product) AS product_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id) = '{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (
                PARTITION BY LAX_STRING(payload.id)
                ORDER BY ingestion_dt DESC, run_id DESC
              ) = 1
            ), p AS (
              SELECT LAX_STRING(payload.ovovTipovilla) AS tipovilla_id
              FROM `{BRONZE}.etendo_Product` p
              JOIN v ON LAX_STRING(p.payload.id) = v.product_id
            )
            SELECT COUNT(*) AS tipovilla_n
            FROM `{BRONZE}.etendo_OV_Tipovilla` t
            JOIN p ON LAX_STRING(t.payload.id) = p.tipovilla_id
            """,
        ),
        (
            "Reserva bronze dup localizador",
            f"""
            SELECT localizador, COUNT(*) AS n
            FROM (
              SELECT LAX_STRING(payload.documentNo) AS localizador
              FROM `{BRONZE}.etendo_Order`
            )
            GROUP BY localizador
            HAVING COUNT(*) > 1
            ORDER BY n DESC
            LIMIT 5
            """,
        ),
        (
            "Order bronze dup by id",
            f"""
            SELECT
              COUNT(*) AS total_rows,
              COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids
            FROM `{BRONZE}.etendo_Order`
            """,
        ),
        (
            "BusinessPartner bronze dup",
            f"""
            SELECT
              COUNT(*) AS total_rows,
              COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids
            FROM `{BRONZE}.etendo_BusinessPartner`
            """,
        ),
        (
            "PricingPriceList bronze dup",
            f"""
            SELECT
              COUNT(*) AS total_rows,
              COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids
            FROM `{BRONZE}.etendo_PricingPriceList`
            """,
        ),
    ]

    for label, sql in checks:
        print("==", label)
        try:
            print(q(c, sql))
        except Exception as exc:
            print("ERR", exc)


if __name__ == "__main__":
    main()
