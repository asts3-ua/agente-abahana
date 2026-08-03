"""Inspect bronze/silver tables to reverse-engineer ETL fixes."""
from __future__ import annotations

import json

from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 100 * 1024 * 1024

BRONZE = f"{PROJECT}.bronze_raw"
SILVER = f"{PROJECT}.silver_clean"
ZEBRA_ID = "5BD6BBB7CF0B415C89A3F23B76392F2B"


def q(client: bigquery.Client, sql: str) -> list[dict]:
    rows = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP),
        location=LOCATION,
    ).result()
    return [dict(r) for r in rows]


def main() -> None:
    client = bigquery.Client(project=PROJECT, location=LOCATION)

    checks = [
        (
            "OBRE_Resource bronze",
            f"""
            SELECT COUNT(*) AS n,
                   COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids
            FROM `{BRONZE}.etendo_OBRE_Resource`
            """,
        ),
        (
            "ZEBRA bronze rows",
            f"""
            SELECT COUNT(*) AS n
            FROM `{BRONZE}.etendo_OBRE_Resource`
            WHERE LAX_STRING(payload.id) = '{ZEBRA_ID}'
            """,
        ),
        (
            "Tarifa silver dup",
            f"""
            SELECT COUNT(*) AS n, COUNT(DISTINCT id) AS unique_ids
            FROM `{SILVER}.stg_etendo_Tarifa`
            """,
        ),
        (
            "TipoTarifa silver dup",
            f"""
            SELECT COUNT(*) AS n, COUNT(DISTINCT id) AS unique_ids
            FROM `{SILVER}.stg_etendo_TipoTarifa`
            """,
        ),
        (
            "Proveedor silver dup",
            f"""
            SELECT COUNT(*) AS n, COUNT(DISTINCT id) AS unique_ids
            FROM `{SILVER}.stg_etendo_Proveedor`
            """,
        ),
        (
            "Tercero silver dup",
            f"""
            SELECT COUNT(*) AS n, COUNT(DISTINCT id) AS unique_ids
            FROM `{SILVER}.stg_etendo_Tercero`
            """,
        ),
        (
            "Reserva silver dup reserva_id",
            f"""
            SELECT COUNT(*) AS n, COUNT(DISTINCT reserva_id) AS unique_ids
            FROM `{SILVER}.stg_etendo_Reserva`
            """,
        ),
        (
            "PricingPriceList bronze",
            f"""
            SELECT COUNT(*) AS n,
                   COUNT(DISTINCT LAX_STRING(payload.id)) AS unique_ids
            FROM `{BRONZE}.etendo_PricingPriceList`
            """,
        ),
    ]

    for label, sql in checks:
        print("==", label)
        try:
            print(q(client, sql))
        except Exception as exc:
            print("ERR", exc)

  # ZEBRA payload keys
    print("== ZEBRA payload keys")
    rows = q(
        client,
        f"""
        SELECT TO_JSON_STRING(payload) AS js
        FROM `{BRONZE}.etendo_OBRE_Resource`
        WHERE LAX_STRING(payload.id) = '{ZEBRA_ID}'
        LIMIT 1
        """,
    )
    if rows:
        payload = json.loads(rows[0]["js"])
        print("keys:", sorted(payload.keys()))

    print("== ZEBRA bronze fields")
    print(
        q(
            client,
            f"""
            SELECT
              LAX_STRING(payload.id) AS id,
              LAX_STRING(payload.name) AS name,
              LAX_STRING(payload.location) AS location,
              LAX_STRING(payload.obovPropietario) AS propietario,
              LAX_BOOL(payload.active) AS active,
              LAX_BOOL(payload.obovVisible) AS visible
            FROM `{BRONZE}.etendo_OBRE_Resource`
            WHERE LAX_STRING(payload.id) = '{ZEBRA_ID}'
            """,
        )
    )

    print("== Villa silver all columns for ZEBRA (distinct combos)")
    villa_cols = [f.name for f in client.get_table(f"{SILVER}.stg_etendo_Villa").schema]
  # only varying cols
    print(
        q(
            client,
            f"""
            SELECT es_visible, pueblo_cercano, propietario_id, location_id, COUNT(*) AS n
            FROM `{SILVER}.stg_etendo_Villa`
            WHERE villa_id = '{ZEBRA_ID}'
            GROUP BY 1,2,3,4
            ORDER BY n DESC
            """,
        )
    )

    print("== BusinessPartnerLocation rows for ZEBRA propietario")
    print(
        q(
            client,
            f"""
            WITH villa AS (
              SELECT LAX_STRING(payload.obovPropietario) AS prop_id
              FROM `{BRONZE}.etendo_OBRE_Resource`
              WHERE LAX_STRING(payload.id) = '{ZEBRA_ID}'
              LIMIT 1
            )
            SELECT COUNT(*) AS n
            FROM `{BRONZE}.etendo_BusinessPartnerLocation` bpl
            CROSS JOIN villa v
            WHERE LAX_STRING(bpl.payload.businessPartner) = v.prop_id
            """,
        )
    )

    print("== stg_etendo_Villa schema")
    print(villa_cols)

    print("== stg_etendo_OBRE_Resource schema")
    print([f.name for f in client.get_table(f"{SILVER}.stg_etendo_OBRE_Resource").schema])

    print("== sample OBRE_Resource silver row count vs bronze")
    print(
        q(
            client,
            f"""
            SELECT
              (SELECT COUNT(*) FROM `{BRONZE}.etendo_OBRE_Resource`) AS bronze_n,
              (SELECT COUNT(*) FROM `{SILVER}.stg_etendo_OBRE_Resource`) AS silver_n,
              (SELECT COUNT(DISTINCT LAX_STRING(payload.id)) FROM `{BRONZE}.etendo_OBRE_Resource`) AS bronze_u,
              (SELECT COUNT(DISTINCT id) FROM `{SILVER}.stg_etendo_OBRE_Resource`) AS silver_u
            """,
        )
    )


if __name__ == "__main__":
    main()
