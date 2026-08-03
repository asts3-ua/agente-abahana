from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT opx_total_camas, opx_capacidad_pax
FROM abahanaweb.silver_clean.stg_etendo_OBRE_Resource
WHERE etendo_raw_id='60326E571FA545868BB8DF412E2615AB'
LIMIT 1
"""
print(list(c.query(sql).result()))
