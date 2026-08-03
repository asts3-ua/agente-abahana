"""Find source of Villa fields for ZEBRA."""
from __future__ import annotations

import json

from google.cloud import bigquery

PROJECT = "abahanaweb"
LOCATION = "EU"
CAP = 500 * 1024 * 1024
BRONZE = f"{PROJECT}.bronze_raw"
ZEBRA = "5BD6BBB7CF0B415C89A3F23B76392F2B"


def main():
    c = bigquery.Client(project=PROJECT, location=LOCATION)
    sql = f"""
    SELECT TO_JSON_STRING(payload) AS js, ingestion_dt, run_id
    FROM `{BRONZE}.etendo_OBRE_Resource`
    WHERE LAX_STRING(payload.id) = '{ZEBRA}'
    ORDER BY ingestion_dt DESC
    """
    rows = list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=CAP), location=LOCATION).result())
    for i, row in enumerate(rows):
        p = json.loads(row.js)
        print(f"--- bronze row {i+1} ingestion={row.ingestion_dt} run={row.run_id}")
        for k in [
            "id", "name", "active", "ovVisible", "abthanZone", "prenuLocation",
            "locationAddress", "product", "resourceCategory", "businessPartner",
            "opxdesRecomendada", "opxdesNovedad",
        ]:
            print(f"  {k}: {p.get(k)}")

    sql2 = f"""
    SELECT es_visible, pueblo_cercano, zona, region, location_id, capacidad_pax,
           rating_exterior, tipovilla_id, dataform_ingestion_dt, dataform_run_id
    FROM `{PROJECT}.silver_clean.stg_etendo_Villa`
    WHERE villa_id = '{ZEBRA}'
    LIMIT 5
    """
    print("\n--- silver villa sample ---")
    for row in c.query(sql2).result():
        print(dict(row))


if __name__ == "__main__":
    main()
