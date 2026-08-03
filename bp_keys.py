import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
row = list(c.query("SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_BusinessPartner LIMIT 1").result())[0]
keys = sorted(json.loads(row.js).keys())
print(keys)
