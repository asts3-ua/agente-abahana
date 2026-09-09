-- Vista silver_clean.stg_etendo_opxdes_ficha_tecnica
--
-- Esta vista NO la gestiona Dataform: no aparece en ningún repositorio de
-- Dataform del proyecto ni en las definiciones de este repo, así que estaba
-- viviendo solo dentro de BigQuery. Se versiona aquí para que quede rastro.
-- Se aplica a mano:
--   bq query --use_legacy_sql=false --project_id=abahanaweb \
--     < dataform/vistas_manuales/stg_etendo_opxdes_ficha_tecnica.sql

CREATE OR REPLACE VIEW `abahanaweb.silver_clean.stg_etendo_opxdes_ficha_tecnica` AS
WITH
  raw_data AS (
  SELECT
    ingestion_dt,
    run_id,
    -- Quitamos PARSE_JSON porque la columna 'payload' ya es tipo JSON en BigQuery
    payload AS json
  FROM
    `abahanaweb.bronze_raw.etendo_opxdes_ficha_tecnica` )
SELECT
  -- Identificadores Clave
  REGEXP_EXTRACT(LAX_STRING(json.`$ref`), r'/([^/]+)$') AS booking_id,
  LAX_STRING(json.id) AS etendo_raw_id,
  LAX_STRING(json.client) AS cliente_id,
  LAX_STRING(json.`client$_identifier`) AS cliente_nombre,
  LAX_STRING(json.organization) AS organizacion_id,
  LAX_STRING(json.`organization$_identifier`) AS organizacion_nombre,

  -- Información de la Propiedad / Reserva
  TRIM(LAX_STRING(json._identifier), " -") AS propiedad_nombre,
  TRIM(REGEXP_EXTRACT(LAX_STRING(json._identifier), r'^[0-9]+\s*-\s*([^-\t\n\r\f\v]+)')) AS propiedad_nombre_limpio,
  CAST(REGEXP_EXTRACT(LAX_STRING(json._identifier), r'^([0-9]+)') AS INT64) AS propiedad_codigo,

  -- LÓGICA DE NORMALIZACIÓN DE LICENCIA TURÍSTICA
  CASE 
    -- 1. Si viene vacío o null, lo dejamos como NULL explicitamente
    WHEN LAX_STRING(json.turismo) IS NULL OR TRIM(LAX_STRING(json.turismo)) = '' THEN NULL
    
    -- 2. Si tiene datos, limpiamos y formateamos
    ELSE 
      CASE 
        -- Verificamos si contiene un patrón numérico de 5 o 6 dígitos
        WHEN REGEXP_CONTAINS(REGEXP_REPLACE(UPPER(LAX_STRING(json.turismo)), r'[^A-Z0-9]', ''), r'VT([0-9]{5,6})A') 
        THEN 
          CONCAT(
            'VT-', 
            REGEXP_EXTRACT(REGEXP_REPLACE(UPPER(LAX_STRING(json.turismo)), r'[^A-Z0-9]', ''), r'VT([0-9]{5,6})A'), 
            '-A'
          )
        -- Si el formato es muy raro y no se puede parsear, dejamos el dato limpio original en mayúsculas
        ELSE UPPER(TRIM(LAX_STRING(json.turismo)))
      END
  END AS licencia_turismo_normalizada,
  LAX_STRING(json.`tipovilla$_identifier`) AS tipo_villa_descripcion,
  CAST(LAX_INT64(json.aNOConstruccion) AS INT64) AS anyo_construccion,
  CAST(LAX_INT64(json.plantas) AS INT64) AS total_plantas,
  CAST(LAX_FLOAT64(json.parcela) AS FLOAT64) AS metros_parcela,
  CAST(LAX_FLOAT64(json.habitable) AS FLOAT64) AS metros_habitables,

  -- Capacidades y Distribución
  CAST(LAX_INT64(json.pAXProd) AS INT64) AS capacidad_pax,
  -- OJO: el campo de origen se llama bEDProd, pero su CONTENIDO es el
  -- número de habitaciones, no de camas: coincide con el recuento de
  -- dormitorios de stg_etendo_Estancia en 197 de 204 villas, y solo en 6
  -- casos equivale a camas_dobles + camas_individuales. Las camas reales
  -- se obtienen sumando por dormitorio en stg_etendo_Estancia.
  CAST(LAX_INT64(json.bEDProd) AS INT64) AS numero_habitaciones,
  CAST(LAX_INT64(json.camadoble) AS INT64) AS camas_dobles,
  CAST(LAX_INT64(json.camaindiv) AS INT64) AS camas_individuales,
  CAST(LAX_INT64(json.banos) AS INT64) AS total_banos,

  -- Comodidades (Estandarización de Booleanos)
  SAFE_CAST(LAX_BOOL(json.internet) AS BOOL) AS tiene_internet,
  SAFE_CAST(LAX_BOOL(json.aircoLiving) AS BOOL) AS tiene_aire_salon,
  SAFE_CAST(LAX_BOOL(json.lavadora) AS BOOL) AS tiene_lavadora,
  SAFE_CAST(LAX_BOOL(json.lavavajillas) AS BOOL) AS tiene_lavavajillas,
  SAFE_CAST(LAX_BOOL(json.piscinaPrivada) AS BOOL) AS tiene_piscina_privada,
  SAFE_CAST(LAX_BOOL(json.animales) AS BOOL) AS admite_animales,

  -- Ubicación y Geografía
  LAX_STRING(json.pueblocercano) AS pueblo_cercano,
  LAX_STRING(json.`zona$_identifier`) AS zona_nombre,
  LAX_STRING(json.`region$_identifier`) AS region_nombre,
  LAX_STRING(json.`location$_identifier`) AS direccion_completa,
  SAFE_CAST(LAX_STRING(json.latitud) AS FLOAT64) AS latitud,
  SAFE_CAST(LAX_STRING(json.longitud) AS FLOAT64) AS longitud,

  -- Ratings / Puntuaciones de Calidad de la Ficha
  CAST(LAX_INT64(json.ratingBanos) AS INT64) AS score_rating_banos,
  CAST(LAX_INT64(json.ratingCocina) AS INT64) AS score_rating_cocina,
  CAST(LAX_INT64(json.ratingInterior) AS INT64) AS score_rating_interior,
  CAST(LAX_INT64(json.ratingExterior) AS INT64) AS score_rating_exterior,

  -- Auditoría y Fechas
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%z', LAX_STRING(json.creationDate)) AS fecha_creacion_sistema,
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%z', LAX_STRING(json.updated)) AS fecha_actualizacion_sistema,
  ingestion_dt AS dataform_ingestion_dt
FROM
  raw_data
