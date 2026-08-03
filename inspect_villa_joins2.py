"""Probe join candidates for Villa duplication."""
from __future__ import annotations

from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 500 * 1024 * 1024
BRONZE = f"{PROJECT}.bronze_raw"
ZEBRA = "5BD6BBB7CF0B415C89A3F23B76392F2B"
LOC = "41486CC24D824F18ABED9E4285D9D39C"


def q(client, sql):
    return [dict(r) for r in client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP), location=LOCATION).result()]


def main():
    c = bigquery.Client(project=PROJECT, location=LOCATION)
    checks = [
        (
            "location bronze rows",
            f"SELECT COUNT(*) n FROM `{BRONZE}.etendo_Location` WHERE LAX_STRING(payload.id)='{LOC}'",
        ),
        (
            "location payload sample",
            f"""
            SELECT
              LAX_STRING(payload.id) AS id,
              LAX_STRING(payload.address1) AS address1,
              LAX_STRING(payload.cityName) AS city,
              LAX_STRING(payload.regionName) AS region
            FROM `{BRONZE}.etendo_Location`
            WHERE LAX_STRING(payload.id)='{LOC}'
            """,
        ),
        (
            "OV_Propietario rows for zebra product",
            f"""
            WITH v AS (
              SELECT LAX_STRING(payload.product) AS product_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id)='{ZEBRA}'
            )
            SELECT COUNT(*) n
            FROM `{BRONZE}.etendo_OV_Propietario` p
            JOIN v ON LAX_STRING(p.payload.id) = v.product_id
               OR LAX_STRING(p.payload.ovovProduct) = v.product_id
            """,
        ),
        (
            "OV_Propietario schema keys sample",
            f"""
            SELECT TO_JSON_STRING(payload) js
            FROM `{BRONZE}.etendo_OV_Propietario`
            LIMIT 1
            """,
        ),
        (
            "product for zebra",
            f"""
            WITH v AS (
              SELECT LAX_STRING(payload.product) AS product_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id)='{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (PARTITION BY LAX_STRING(payload.id) ORDER BY ingestion_dt DESC)=1
            )
            SELECT
              LAX_STRING(p.payload.id) AS product_id,
              LAX_STRING(p.payload.ovovLocation) AS ovov_location,
              LAX_STRING(p.payload.ovovTipovilla) AS tipovilla_id,
              LAX_STRING(p.payload.name) AS product_name
            FROM `{BRONZE}.etendo_Product` p
            JOIN v ON LAX_STRING(p.payload.id)=v.product_id
            """,
        ),
        (
            "plantas rows for zebra product",
            f"""
            WITH v AS (
              SELECT LAX_STRING(payload.product) AS product_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id)='{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (PARTITION BY LAX_STRING(payload.id) ORDER BY ingestion_dt DESC)=1
            )
            SELECT COUNT(*) n
            FROM `{BRONZE}.etendo_OV_Plantas` pl
            JOIN v ON LAX_STRING(pl.payload.id)=v.product_id
               OR LAX_STRING(pl.payload.ovovProduct)=v.product_id
            """,
        ),
        (
            "banios rows for zebra product",
            f"""
            WITH v AS (
              SELECT LAX_STRING(payload.product) AS product_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id)='{ZEBRA}'
              QUALIFY ROW_NUMBER() OVER (PARTITION BY LAX_STRING(payload.id) ORDER BY ingestion_dt DESC)=1
            )
            SELECT COUNT(*) n
            FROM `{BRONZE}.etendo_OV_Banios` b
            JOIN v ON LAX_STRING(b.payload.ovovProduct)=v.product_id
            """,
        ),
        (
            "resource category rows for zebra",
            f"""
            SELECT COUNT(*) n
            FROM `{BRONZE}.etendo_OBRE_Resource`
            WHERE LAX_STRING(payload.id)='{ZEBRA}'
            """,
        ),
        (
            "compare silver OBRE resource zebra",
            f"""
            SELECT COUNT(*) n
            FROM `{PROJECT}.silver_clean.stg_etendo_OBRE_Resource`
            WHERE etendo_raw_id='{ZEBRA}'
            """,
        ),
    ]
    for label, sql in checks:
        print("==", label)
        try:
            rows = q(c, sql)
            for row in rows:
                if "js" in row and row["js"]:
                    import json
                    keys = sorted(json.loads(row["js"]).keys())
                    print("keys", keys[:30])
                else:
                    print(row)
        except Exception as exc:
            print("ERR", exc)


if __name__ == "__main__":
    main()
