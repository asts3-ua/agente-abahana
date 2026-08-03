import json
from google.cloud import bigquery
from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
prod_id = "0C497896A7374EB499F19E36EC83EC11"

queries = [
    ("ficha by product field", f"""
      SELECT COUNT(*) n FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
      WHERE LAX_STRING(payload.product) = '{prod_id}'
    """),
    ("ficha by id = product", f"""
      SELECT COUNT(*) n FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
      WHERE LAX_STRING(payload.id) = '{prod_id}'
    """),
    ("propietario for product bpartner", f"""
      WITH prod AS (
        SELECT payload FROM abahanaweb.bronze_raw.etendo_Product
        WHERE LAX_STRING(payload.id)='{prod_id}'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY LAX_STRING(payload.id) ORDER BY ingestion_dt DESC)=1
      )
      SELECT COUNT(*) n
      FROM abahanaweb.bronze_raw.etendo_OV_Propietario p, prod
      WHERE LAX_STRING(p.payload.bpartner) = LAX_STRING(prod.payload.id)
    """),
    ("propietario opxdesFicha join ficha count", f"""
      SELECT COUNT(*) n
      FROM abahanaweb.bronze_raw.etendo_OV_Propietario p
      JOIN abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
        ON LAX_STRING(f.payload.id) = LAX_STRING(p.payload.opxdesFicha)
    """),
    ("propietario for paradise resource product only", f"""
      SELECT COUNT(*) n
      FROM abahanaweb.bronze_raw.etendo_OV_Propietario p
      WHERE LAX_STRING(p.payload.bpartner) = '{prod_id}'
         OR LAX_STRING(p.payload.id) = '{prod_id}'
    """),
]
for label, sql in queries:
    print(label, list(c.query(sql, job_config=QueryJobConfig(maximum_bytes_billed=cap)).result()))

# sample ficha row structure - any row
sql = """
SELECT TO_JSON_STRING(payload) js
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
WHERE LAX_STRING(payload.id) IS NOT NULL
LIMIT 1
"""
row = list(c.query(sql).result())[0]
f = json.loads(row.js)
print('\nSample ficha product field:', f.get('product'))
print('Sample ficha id:', f.get('id'))
for k in sorted(f.keys()):
    if 'product' in k.lower() or 'villa' in k.lower() or 'resource' in k.lower() or k.startswith('ov'):
        print(k, f.get(k))
