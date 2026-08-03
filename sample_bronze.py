import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024

samples = [
    ("PricingPriceList", "SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_PricingPriceList LIMIT 1"),
    ("Order", "SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_Order WHERE LAX_STRING(payload.documentNo) LIKE '2026_%' LIMIT 1"),
    ("ProductCategory", "SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_ProductCategory LIMIT 1"),
    ("UOM", "SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_UOM LIMIT 1"),
    ("ficha zebra", """
      SELECT TO_JSON_STRING(f.payload) js
      FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
      WHERE LAX_STRING(f.payload.product) = 'E8FD54F33BB44CA2AE0E58C6DACFAC27'
      LIMIT 1
    """),
]
for name, sql in samples:
    print("==", name)
    try:
        rows = list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result())
        if not rows:
            print("(empty)")
            continue
        keys = sorted(json.loads(rows[0].js).keys())
        print("keys:", keys)
        for k in keys[:15]:
            p = json.loads(rows[0].js)
            print(f"  {k}: {p.get(k)}")
    except Exception as e:
        print("ERR", e)
