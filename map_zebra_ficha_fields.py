import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
sql = """
SELECT TO_JSON_STRING(payload) js
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
WHERE LAX_STRING(payload.obreResource)='5BD6BBB7CF0B415C89A3F23B76392F2B'
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY LAX_STRING(payload.id)
  ORDER BY ingestion_dt DESC, run_id DESC
)=1
LIMIT 1
"""
f = json.loads(list(c.query(sql).result())[0].js)
for k in sorted(f.keys()):
    if any(x in k.lower() for x in ['pobla','loc','zona','region','visible','lat','long','dire','pax','camas','bano','plant','m2','piscina','rating','tipovilla']):
        print(k, f.get(k))

loc = f.get('ovLocation') or f.get('location')
print('location id', loc)
if loc:
    sql2 = f"""
    SELECT LAX_STRING(payload.cityName) city, COUNT(*) n
    FROM abahanaweb.bronze_raw.etendo_Location
    WHERE LAX_STRING(payload.id)='{loc}'
    GROUP BY 1
    """
    print('location rows', list(c.query(sql2).result()))
