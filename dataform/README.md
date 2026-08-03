# ETLs corregidas para `silver_clean`

Archivos listos para copiar a tu workspace de **Dataform** (consola GCP).

## Qué se ha corregido

### Problema común (todas las tablas)
El bronze guarda **varias ingestas del mismo `id`** (histórico de runs). Las ETLs antiguas no deduplicaban y multiplicaban filas.

**Fix aplicado** en todas:
```sql
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY LAX_STRING(payload.id)
  ORDER BY ingestion_dt DESC, run_id DESC
) = 1
```

### `stg_etendo_Villa` (crítico — ZEBRA x18)
Dos bugs:
1. **Sin dedupe** en bronze → 3 snapshots del mismo recurso.
2. **Join incorrecto** propietario → ficha sin filtrar por villa → producto cartesiano (~895 filas por villa).

**Fix:**
- Ficha unida por `ficha.obreResource = resource.id` (no al revés).
- Propietario unido solo a la ficha de esa villa.
- Dedupe en resource, ficha, tipovilla, location, propietario.
- `ROW_NUMBER()` final: **1 fila por `villa_id`**.

Resultado esperado: **542 → ~230 filas** (1 por villa real).

### `stg_etendo_Reserva`
- Dedupe por `order.id`.
- Mapeo explícito de estados (`CO`→`Completada`, `RE`→`Reserva`, etc.).
- `villa_id` = `opxdesResource` (id de recurso, coherente con Villa).

### `stg_etendo_Proveedor`, `Tercero`, `Tarifa`, `TipoTarifa`, `TipoProducto`, `TipoUnidad`
- Mismo patrón de dedupe.
- Proveedor: tu versión + dedupe.

## Cómo aplicar en Dataform

1. Abre cada `.sqlx` en `dataform/definitions/`.
2. Sustituye el contenido del archivo correspondiente en la consola de Dataform.
3. **Compila** y revisa el plan (no debería romper dependencias si los `ref()` son iguales).
4. **Ejecuta** las tablas afectadas.
5. Valida con:
   ```powershell
   $env:SSL_CERT_FILE = ".\tools\windows-ca-bundle.pem"
   $env:REQUESTS_CA_BUNDLE = $env:SSL_CERT_FILE
   .\.venv\Scripts\python.exe audit_silver.py
   ```

## Nota sobre `stg_etendo_OBRE_Resource`

Se ha regenerado con dedupe. Si tu versión actual mapea más campos del payload, **conserva tus columnas** y solo añade el bloque `QUALIFY ROW_NUMBER()` al CTE `raw_data`.

## Limitación

No puedo editar Dataform directamente desde aquí (el repositorio vive en la consola GCP, no en este proyecto). Los archivos están en `dataform/definitions/` para que los copies o conectes el repo git a este directorio.
