import json
from google.cloud import bigquery

c = bigquery.Client(project="abahanaweb", location="EU")
cap = 500 * 1024 * 1024
prod_id = "0C497896A7374EB499F19E36EC83EC11"
res_id = "60326E571FA545868BB8DF412E2615AB"

for label, sql in [
    ("ficha id=product", f"SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica WHERE LAX_STRING(payload.id)='{prod_id}' LIMIT 1"),
    ("ficha obreResource=resource", f"SELECT TO_JSON_STRING(payload) js FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica WHERE LAX_STRING(payload.obreResource)='{res_id}' LIMIT 1"),
    ("count ficha per resource paradise", f"SELECT COUNT(*) n FROM abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica WHERE LAX_STRING(payload.obreResource)='{res_id}'"),
    ("count propietario joined all ficha", f"SELECT COUNT(*) n FROM abahanaweb.bronze_raw.etendo_OV_Propietario p JOIN abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f ON LAX_STRING(f.payload.id)=LAX_STRING(p.payload.opxdesFicha) JOIN abahanaweb.bronze_raw.etendo_OBRE_Resource r ON LAX_STRING(r.payload.id)='{res_id}'"),
    ("count propietario filtered by ficha.obreResource", f"""
      SELECT COUNT(*) n
      FROM abahanaweb.bronze_raw.etendo_OBRE_Resource r
      JOIN abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica f
        ON LAX_STRING(f.payload.obreResource) = LAX_STRING(r.payload.id)
      LEFT JOIN abahanaweb.bronze_raw.etendo_OV_Propietario p
        ON LAX_STRING(p.payload.opxdesFicha) = LAX_STRING(f.payload.id)
      WHERE LAX_STRING(r.payload.id)='{res_id}'
    """),
]:
    print('==', label)
    rows = list(c.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap)).result())
    if rows and hasattr(rows[0], 'js'):
        f = json.loads(rows[0].js)
        print('obreResource', f.get('obreResource'), 'tipovilla', f.get('tipovilla'))
        for k in ['ovjchCapacidadpax','ovRatingExterior','ovRatingInterior','ovRatingVistas','ovjchPiscinaprivada','ovjchPiscinacomun','ovjchPiscinaclimatizada','ovjchJardin','ovjchGaraje','ovjchAnimales','ovjchMetrosconstruidos','ovjchMetrosparcela','ovjchAnyoconstruccion','ovjchNumplantas','ovjchNumbanos','ovjchCamas','ovPoblacion','ovZona','ovRegion','ovLatitud','ovLongitud','ovDireccion','ovLocation']:
            if k in f:
                print(f'  {k}: {f.get(k)}')
    else:
        print(rows)
