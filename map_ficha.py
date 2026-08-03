import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
prod_id = "0C497896A7374EB499F19E36EC83EC11"
sql = f"""
SELECT TO_JSON_STRING(payload) js
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
WHERE LAX_STRING(payload.product) = '{prod_id}'
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY LAX_STRING(payload.id)
  ORDER BY ingestion_dt DESC, run_id DESC
) = 1
"""
rows = list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result())
print('ficha rows', len(rows))
if rows:
    ficha = json.loads(rows[0].js)
    print('keys', sorted(ficha.keys()))
    for k in sorted(ficha.keys()):
        print(f"{k}: {ficha.get(k)}")

# maybe join by opxdesFicha from propietario or resource
sql2 = """
SELECT COUNT(*) n
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
JOIN abahanaweb.bronze_raw.etendo_OV_Propietario p
  ON LAX_STRING(f.payload.id) = LAX_STRING(p.payload.opxdesFicha)
JOIN abahanaweb.bronze_raw.etendo_OBRE_Resource r
  ON LAX_STRING(r.payload.product) = @prod
WHERE LAX_STRING(r.payload.id) = '60326E571FA545868BB8DF412E2615AB'
"""
from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter
rows2 = list(c.query(sql2, job_config=QueryJobConfig(
    maximum_bytes_billed=cap,
    query_parameters=[ScalarQueryParameter('prod','STRING', prod_id)]
)).result())
print('ficha via propietario', rows2)
