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


  -- Vistas
  SAFE_CAST(LAX_BOOL(json.vistaMar) AS BOOL) AS tiene_vista_mar,
  SAFE_CAST(LAX_BOOL(json.vistaPanomar) AS BOOL) AS tiene_vista_panoramica_mar,
  SAFE_CAST(LAX_BOOL(json.vistaDespe) AS BOOL) AS tiene_vista_despejada,
  SAFE_CAST(LAX_BOOL(json.vistaMonte) AS BOOL) AS tiene_vista_montana,
  SAFE_CAST(LAX_BOOL(json.vistaCampo) AS BOOL) AS tiene_vista_campo,
  SAFE_CAST(LAX_BOOL(json.vistaLim) AS BOOL) AS tiene_vista_limitada,

  -- Carácter de la zona
  SAFE_CAST(LAX_BOOL(json.zonaTranquila) AS BOOL) AS zona_tranquila,
  SAFE_CAST(LAX_BOOL(json.zonaAnimada) AS BOOL) AS zona_animada,
  SAFE_CAST(LAX_BOOL(json.zonaCentrica) AS BOOL) AS zona_centrica,
  SAFE_CAST(LAX_BOOL(json.zonaAislada) AS BOOL) AS zona_aislada,
  SAFE_CAST(LAX_BOOL(json.zonaCampo) AS BOOL) AS zona_campo,
  SAFE_CAST(LAX_BOOL(json.urbresidencial) AS BOOL) AS urbanizacion_residencial,
  SAFE_CAST(LAX_BOOL(json.cercamar) AS BOOL) AS cerca_del_mar,

  -- Distancias en metros
  SAFE_CAST(LAX_INT64(json.distAlmar) AS INT64) AS distancia_mar_m,
  SAFE_CAST(LAX_INT64(json.playaArenafina) AS INT64) AS distancia_playa_arena_m,
  SAFE_CAST(LAX_INT64(json.playaCalita) AS INT64) AS distancia_cala_m,
  SAFE_CAST(LAX_INT64(json.playaMinusvalido) AS INT64) AS distancia_playa_adaptada_m,
  SAFE_CAST(LAX_INT64(json.distSuper) AS INT64) AS distancia_supermercado_m,
  SAFE_CAST(LAX_INT64(json.distResto) AS INT64) AS distancia_restaurantes_m,
  SAFE_CAST(LAX_INT64(json.distGolf) AS INT64) AS distancia_golf_m,
  SAFE_CAST(LAX_INT64(json.distTennis) AS INT64) AS distancia_tenis_m,
  SAFE_CAST(LAX_INT64(json.distNautico) AS INT64) AS distancia_club_nautico_m,
  SAFE_CAST(LAX_INT64(json.distHipico) AS INT64) AS distancia_hipica_m,
  SAFE_CAST(LAX_INT64(json.distMedico) AS INT64) AS distancia_centro_medico_m,
  SAFE_CAST(LAX_INT64(json.distFarmacia) AS INT64) AS distancia_farmacia_m,
  SAFE_CAST(LAX_INT64(json.distBanco) AS INT64) AS distancia_banco_m,
  SAFE_CAST(LAX_INT64(json.distPueblocercano) AS INT64) AS distancia_pueblo_m,
  SAFE_CAST(LAX_INT64(json.distAeroAlicante) AS INT64) AS distancia_aeropuerto_alicante_m,
  SAFE_CAST(LAX_INT64(json.distAeroValencia) AS INT64) AS distancia_aeropuerto_valencia_m,

  -- Piscina
  SAFE_CAST(LAX_INT64(json.piscinaPrivadaLar) AS INT64) AS piscina_largo_m,
  SAFE_CAST(LAX_INT64(json.piscinaPrivadaLon) AS INT64) AS piscina_ancho_m,
  SAFE_CAST(LAX_INT64(json.profundidadMinPiscina) AS INT64) AS piscina_profundidad_min_m,
  SAFE_CAST(LAX_INT64(json.profundidadMaxPiscina) AS INT64) AS piscina_profundidad_max_m,
  SAFE_CAST(LAX_BOOL(json.piscinaNinos) AS BOOL) AS tiene_piscina_infantil,
  SAFE_CAST(LAX_BOOL(json.piscinaClimatizada) AS BOOL) AS piscina_climatizada,
  SAFE_CAST(LAX_BOOL(json.piscinaComun) AS BOOL) AS tiene_piscina_comun,

  -- Baños y estancias
  SAFE_CAST(LAX_INT64(json.banosBanera) AS INT64) AS banos_con_banera,
  SAFE_CAST(LAX_INT64(json.banosDucha) AS INT64) AS banos_con_ducha,
  SAFE_CAST(LAX_INT64(json.aseo) AS INT64) AS numero_aseos,
  SAFE_CAST(LAX_INT64(json.ensuite) AS INT64) AS banos_en_suite,
  SAFE_CAST(LAX_INT64(json.salonComedor) AS INT64) AS salones_comedor,
  SAFE_CAST(LAX_INT64(json.salon) AS INT64) AS salones,

  -- Climatización
  SAFE_CAST(LAX_INT64(json.aircofrio) AS INT64) AS equipos_aire_frio,
  SAFE_CAST(LAX_INT64(json.aircohot) AS INT64) AS equipos_aire_calor,
  SAFE_CAST(LAX_INT64(json.aircoBed) AS INT64) AS aire_dormitorios,
  SAFE_CAST(LAX_INT64(json.aircoLiving) AS INT64) AS aire_salon_equipos,
  SAFE_CAST(LAX_INT64(json.ovjchNumAacc) AS INT64) AS equipos_aire_acondicionado,
  SAFE_CAST(LAX_BOOL(json.bombacalor) AS BOOL) AS tiene_bomba_calor,
  SAFE_CAST(LAX_BOOL(json.sueloradiante) AS BOOL) AS tiene_suelo_radiante,
  SAFE_CAST(LAX_BOOL(json.chimenea) AS BOOL) AS tiene_chimenea,
  SAFE_CAST(LAX_BOOL(json.radiador) AS BOOL) AS tiene_radiadores,

  -- Cocina y electrodomésticos
  SAFE_CAST(LAX_BOOL(json.horno) AS BOOL) AS tiene_horno,
  SAFE_CAST(LAX_BOOL(json.microondas) AS BOOL) AS tiene_microondas,
  SAFE_CAST(LAX_BOOL(json.congelador) AS BOOL) AS tiene_congelador,
  SAFE_CAST(LAX_BOOL(json.frigo) AS BOOL) AS tiene_frigorifico,
  SAFE_CAST(LAX_BOOL(json.induccion) AS BOOL) AS tiene_induccion,
  SAFE_CAST(LAX_BOOL(json.placaVitro) AS BOOL) AS tiene_vitroceramica,
  SAFE_CAST(LAX_BOOL(json.plancha) AS BOOL) AS tiene_plancha,
  SAFE_CAST(LAX_BOOL(json.secadora) AS BOOL) AS tiene_secadora,
  SAFE_CAST(LAX_INT64(json.cocinaAmericana) AS INT64) AS cocinas_americanas,
  SAFE_CAST(LAX_INT64(json.cocinaSeparada) AS INT64) AS cocinas_separadas,
  SAFE_CAST(LAX_INT64(json.cocinaExterior) AS INT64) AS cocinas_exteriores,
  LAX_STRING(json.ovjchCafeteras) AS tipo_cafetera,

  -- Exterior
  SAFE_CAST(LAX_BOOL(json.terrazaCubierta) AS BOOL) AS tiene_terraza_cubierta,
  SAFE_CAST(LAX_BOOL(json.terrazaDescubierta) AS BOOL) AS tiene_terraza_descubierta,
  SAFE_CAST(LAX_BOOL(json.duchaExt) AS BOOL) AS tiene_ducha_exterior,
  SAFE_CAST(LAX_BOOL(json.bbq) AS BOOL) AS tiene_barbacoa,
  LAX_STRING(json.ovjchBarbacoa) AS tipo_barbacoa,
  LAX_STRING(json.bBQPortatil) AS barbacoa_portatil,
  SAFE_CAST(LAX_BOOL(json.cesped) AS BOOL) AS tiene_cesped,
  SAFE_CAST(LAX_BOOL(json.arbolado) AS BOOL) AS tiene_arbolado,
  SAFE_CAST(LAX_BOOL(json.solarium) AS BOOL) AS tiene_solarium,
  SAFE_CAST(LAX_BOOL(json.jardin) AS BOOL) AS tiene_jardin,

  -- Ocio y deporte
  SAFE_CAST(LAX_BOOL(json.ovjchGimnasio) AS BOOL) AS tiene_gimnasio,
  SAFE_CAST(LAX_BOOL(json.sauna) AS BOOL) AS tiene_sauna,
  SAFE_CAST(LAX_BOOL(json.jacuzzi) AS BOOL) AS tiene_jacuzzi,
  SAFE_CAST(LAX_BOOL(json.hidromasaje) AS BOOL) AS tiene_hidromasaje,
  SAFE_CAST(LAX_BOOL(json.ovjchBillar) AS BOOL) AS tiene_billar,
  SAFE_CAST(LAX_BOOL(json.ovjchPingpong) AS BOOL) AS tiene_pingpong,
  SAFE_CAST(LAX_BOOL(json.ovjchTenis) AS BOOL) AS tiene_pista_tenis,
  SAFE_CAST(LAX_BOOL(json.ovjchPadel) AS BOOL) AS tiene_pista_padel,
  SAFE_CAST(LAX_BOOL(json.ovjchColumpio) AS BOOL) AS tiene_columpio,
  SAFE_CAST(LAX_BOOL(json.ovjchTobogan) AS BOOL) AS tiene_tobogan,
  SAFE_CAST(LAX_BOOL(json.ovjchFutbolin) AS BOOL) AS tiene_futbolin,
  SAFE_CAST(LAX_BOOL(json.ovjchCamaelastica) AS BOOL) AS tiene_cama_elastica,

  -- Accesibilidad
  SAFE_CAST(LAX_BOOL(json.minusvalidos) AS BOOL) AS apto_movilidad_reducida,
  SAFE_CAST(LAX_BOOL(json.ovjchSillaruedas) AS BOOL) AS accesible_silla_ruedas,

  -- Parking
  SAFE_CAST(LAX_INT64(json.numpcalle) AS INT64) AS plazas_calle,
  SAFE_CAST(LAX_INT64(json.numpcubierto) AS INT64) AS plazas_cubiertas,
  SAFE_CAST(LAX_INT64(json.numpdescubierto) AS INT64) AS plazas_descubiertas,
  SAFE_CAST(LAX_INT64(json.numpgaraje) AS INT64) AS plazas_garaje,
  SAFE_CAST(LAX_BOOL(json.garaje) AS BOOL) AS tiene_garaje,

  -- Multimedia
  SAFE_CAST(LAX_BOOL(json.tv) AS BOOL) AS tiene_tv,
  SAFE_CAST(LAX_BOOL(json.ovjchSmarttv) AS BOOL) AS tiene_smart_tv,
  SAFE_CAST(LAX_BOOL(json.satelite) AS BOOL) AS tv_satelite,
  SAFE_CAST(LAX_BOOL(json.ovjchAltavoz) AS BOOL) AS tiene_altavoz,

  -- Valoraciones que faltaban
  SAFE_CAST(LAX_INT64(json.ratingVistas) AS INT64) AS score_rating_vistas,
  SAFE_CAST(LAX_INT64(json.ratingPrivacidad) AS INT64) AS score_rating_privacidad,
  SAFE_CAST(LAX_INT64(json.ratingTranquilidad) AS INT64) AS score_rating_tranquilidad,
  SAFE_CAST(LAX_INT64(json.ratingDistanciaalmar) AS INT64) AS score_rating_distancia_mar,

  -- Licencia turística y cumplimiento (uso interno)
  LAX_STRING(json.turismo) AS licencia_vut,
  LAX_STRING(json.ovjchCodEstablecimiento) AS codigo_establecimiento,
  SAFE.PARSE_DATE('%Y-%m-%d', LAX_STRING(json.ovjchFechaAntiguedadVut)) AS vut_fecha_antiguedad,
  SAFE.PARSE_DATE('%Y-%m-%d', LAX_STRING(json.ovjchFechaCaducidadVut)) AS vut_fecha_caducidad,
  SAFE_CAST(LAX_BOOL(json.ovjchBajaRegTurismo) AS BOOL) AS vut_baja_registro,
  SAFE.PARSE_DATE('%Y-%m-%d', LAX_STRING(json.ovjchFBajaRegTur)) AS vut_fecha_baja,
  SAFE_CAST(LAX_BOOL(json.ovjchAltaSesHospedaje) AS BOOL) AS alta_ses_hospedaje,
  SAFE.PARSE_DATE('%Y-%m-%d', LAX_STRING(json.ovjchFAltaSesHos)) AS fecha_alta_ses_hospedaje,

  -- Comercial (uso interno)
  SAFE_CAST(LAX_INT64(json.importeFianza) AS INT64) AS importe_fianza,
  SAFE_CAST(LAX_BOOL(json.topvilla) AS BOOL) AS es_top_villa,
  SAFE_CAST(LAX_INT64(json.ovComisionWr) AS INT64) AS comision_pct,
  SAFE_CAST(LAX_BOOL(json.reformada) AS BOOL) AS esta_reformada,
  LAX_STRING(json.ovjchAnyoReforma) AS anyo_reforma,
  SAFE_CAST(LAX_INT64(json.classification) AS INT64) AS clasificacion,
  LAX_STRING(json.ovDecorator) AS decorador,
  LAX_STRING(json.ovDesigner) AS interiorista,

  -- Acceso y seguridad (uso interno).
  -- OJO: contiene códigos de alarma, ubicación de la caja fuerte y
  -- credenciales de wifi. Solo debe llegar a los roles interno y admin, y
  -- solo cuando el usuario lo pida explícitamente
  SAFE_CAST(LAX_BOOL(json.ovjchAlarma) AS BOOL) AS tiene_alarma,
  LAX_STRING(json.ovjchActivacionAlarma) AS alarma_activacion,
  LAX_STRING(json.ovjchDesactivacionAlarma) AS alarma_desactivacion,
  LAX_STRING(json.ovjchUsoalarma) AS alarma_uso,
  SAFE_CAST(LAX_BOOL(json.ovjchCajafuerte) AS BOOL) AS tiene_caja_fuerte,
  LAX_STRING(json.ovjchCajaAutomatico) AS caja_fuerte_ubicacion,
  LAX_STRING(json.ovjchRedwifi) AS wifi_red,
  LAX_STRING(json.ovjchProveedorWifi) AS wifi_proveedor,
  LAX_STRING(json.ovjchUbicacionRouter) AS wifi_router_ubicacion,
  LAX_STRING(json.ovjchTipowifi) AS wifi_tipo,
  LAX_STRING(json.acceso) AS tipo_acceso,
  LAX_STRING(json.ovjchAccesoCoche) AS acceso_coche,
  LAX_STRING(json.ovjchPuertaParking) AS puerta_parking,
  LAX_STRING(json.ovjchDesAccpeatonal) AS acceso_peatonal_desc,
  LAX_STRING(json.ovjchEscaleras) AS escaleras,
  LAX_STRING(json.ovjchDesPlantas) AS plantas_desc,

  -- Auditoría y Fechas
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%z', LAX_STRING(json.creationDate)) AS fecha_creacion_sistema,
  SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%z', LAX_STRING(json.updated)) AS fecha_actualizacion_sistema,
  ingestion_dt AS dataform_ingestion_dt
FROM
  raw_data
