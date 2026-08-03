from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT
  LAX_STRING(payload.id) AS ficha_id,
  LAX_STRING(payload.obreResource) AS resource_id,
  LAX_STRING(payload.ovPoblacion) AS pueblo,
  SAFE_CAST(LAX_BOOL(payload.ovVisible) AS BOOL) AS visible,
  ingestion_dt,
  run_id
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
WHERE LAX_STRING(payload.obreResource) = '5BD6BBB7CF0B415C89A3F23B76392F2B'
ORDER BY ingestion_dt DESC
"""
for row in c.query(sql).result():
    print(dict(row))
