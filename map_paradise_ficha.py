import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT TO_JSON_STRING(payload) js
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
WHERE LAX_STRING(payload.id)='0C497896A7374EB499F19E36EC83EC11'
LIMIT 1
"""
f = json.loads(list(c.query(sql).result())[0].js)
for k in sorted(f.keys()):
  if any(x in k.lower() for x in ['m2','metro','cama','habit','parcel','constr','animal','jardin','garaje','piscina','propiet']):
    print(k, f.get(k))
