import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT TO_JSON_STRING(payload) js
FROM abahanaweb.bronze_raw.etendo_OBRE_Resource
WHERE LAX_STRING(payload.id)='60326E571FA545868BB8DF412E2615AB'
QUALIFY ROW_NUMBER() OVER (PARTITION BY LAX_STRING(payload.id) ORDER BY ingestion_dt DESC)=1
"""
res = json.loads(list(c.query(sql).result())[0].js)
sql2 = """
SELECT * FROM abahanaweb.silver_clean.stg_etendo_OBRE_Resource
WHERE etendo_raw_id='60326E571FA545868BB8DF412E2615AB'
LIMIT 1
"""
silver = dict(list(c.query(sql2).result())[0])
print('silver columns sample:')
for k,v in silver.items():
    if v is not None:
        print(k, '=', v)
