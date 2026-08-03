from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
ZEBRA = "5BD6BBB7CF0B415C89A3F23B76392F2B"
queries = [
    ("ficha count zebra", f"SELECT COUNT(*) n FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica WHERE LAX_STRING(payload.obreResource)='{ZEBRA}'"),
    ("bad join count", f"""
      SELECT COUNT(*) n
      FROM abahanaweb.bronze_raw.etendo_OBRE_Resource r
      JOIN abahanaweb.bronze_raw.etendo_OV_Propietario p ON TRUE
      JOIN abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f ON LAX_STRING(f.payload.id)=LAX_STRING(p.payload.opxdesFicha)
      WHERE LAX_STRING(r.payload.id)='{ZEBRA}'
    """),
    ("bad join without true", f"""
      SELECT COUNT(*) n
      FROM abahanaweb.bronze_raw.etendo_OBRE_Resource r
      JOIN abahanaweb.bronze_raw.etendo_OV_Propietario p
        ON LAX_STRING(p.payload.opxdesFicha) IS NOT NULL
      JOIN abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
        ON LAX_STRING(f.payload.id)=LAX_STRING(p.payload.opxdesFicha)
      WHERE LAX_STRING(r.payload.id)='{ZEBRA}'
    """),
    ("good join count latest resource", f"""
      WITH r AS (
        SELECT payload FROM abahanaweb.bronze_raw.etendo_OBRE_Resource
        WHERE LAX_STRING(payload.id)='{ZEBRA}'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY LAX_STRING(payload.id) ORDER BY ingestion_dt DESC)=1
      )
      SELECT COUNT(*) n
      FROM r
      JOIN abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
        ON LAX_STRING(f.payload.obreResource)=LAX_STRING(r.payload.id)
      LEFT JOIN abahanaweb.bronze_raw.etendo_OV_Propietario p
        ON LAX_STRING(p.payload.opxdesFicha)=LAX_STRING(f.payload.id)
    """),
]
for label, sql in queries:
    print(label, list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result()))
