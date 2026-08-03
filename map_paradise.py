import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
VID = "60326E571FA545868BB8DF412E2615AB"
cap = 500 * 1024 * 1024

def latest(table, id_field="id", id_val=VID):
    sql = f"""
    SELECT TO_JSON_STRING(payload) js
    FROM abahanaweb.bronze_raw.{table}
    WHERE LAX_STRING(payload.{id_field}) = '{id_val}'
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY LAX_STRING(payload.{id_field})
      ORDER BY ingestion_dt DESC, run_id DESC
    ) = 1
    LIMIT 1
    """
    rows = list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result())
    return json.loads(rows[0].js) if rows else {}

res = latest("etendo_OBRE_Resource")
prod_id = res.get("product")
prod = latest("etendo_Product", "id", prod_id) if prod_id else {}
loc_id = prod.get("ovovLocation")
loc = latest("etendo_Location", "id", loc_id) if loc_id else {}
tip_id = prod.get("ovovTipovilla")
tip = latest("etendo_OV_Tipovilla", "id", tip_id) if tip_id else {}

print("resource.product", prod_id)
print("product.ovovLocation", loc_id)
print("product.ovovTipovilla", tip_id)
for label, obj, keys in [
    ("resource", res, ["id","name","searchKey","active","ovVisible","opxdesRecomendada","opxdesNovedad","creationDate","updated"]),
    ("product", prod, ["id","name","searchKey"]),
    ("location", loc, ["id","address1","cityName","regionName","latitude","longitude"]),
    ("tipovilla", tip, ["id","name","description"]),
]:
    print(f"\n{label}:")
    for k in keys:
        print(f"  {k}: {obj.get(k)}")

# ficha by product
sql = f"""
SELECT TO_JSON_STRING(payload) js
FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica
WHERE LAX_STRING(payload.product) = '{prod_id}'
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY LAX_STRING(payload.id)
  ORDER BY ingestion_dt DESC, run_id DESC
) = 1
LIMIT 1
"""
rows = list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result())
if rows:
    ficha = json.loads(rows[0].js)
    print("\nficha keys sample:")
    for k in sorted(ficha.keys()):
        if any(x in k.lower() for x in ["pax","camas","bano","planta","m2","piscina","jardin","garaje","animal","rating","pueblo","zona","region","ano","climat"]):
            print(f"  {k}: {ficha.get(k)}")
