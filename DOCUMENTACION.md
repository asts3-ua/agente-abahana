# Agente IA — Abahana Villas
## Asistente virtual para consultas de villas vacacionales

---

## 1. ¿Qué es este proyecto?

Un **asistente virtual inteligente** que responde preguntas en lenguaje natural sobre las villas de alquiler vacacional de Abahana Villas. El usuario escribe preguntas como si hablara con una persona, y el agente consulta la base de datos real y responde con información actualizada.

### Ejemplos de uso

```
Usuario: "¿Qué villas tienen piscina en Calpe para 6 personas?"
Agente:  "He encontrado 20 villas en Calpe con piscina para 6 o más personas:
          - 1005 ALFALS | Calpe | 6 personas | Piscina: Sí
          - 1017 CASAVERANO | Calpe | 10 personas | Piscina: Sí ..."

Usuario: "Quiero una villa que admita perros con piscina"
Agente:  "He encontrado 20 villas que admiten animales con piscina:
          - 1017 CASAVERANO | Calpe | 10 personas | Animales: Sí ..."

Usuario: "Muéstrame todas las villas disponibles"
Agente:  "Tenemos un total de 891 propiedades. Aquí tienes algunas: ..."
```

**Estado actual: funcional con datos reales.** 891 propiedades del catálogo real de Abahana Villas desde la capa Silver de BigQuery.

---

## 2. Tecnologías utilizadas

### Google BigQuery — La base de datos
- **¿Qué es?** Almacén de datos en la nube de Google.
- **¿Qué guarda?** Capa Silver (`silver_clean`) del data warehouse de Abahana, proyecto `abahanaweb`.
- **Tablas usadas:**
  - `dim_propiedades` — catálogo limpio de propiedades (1 fila por villa)
  - `int_etendo_bookings` — bookings enriquecidos con datos completos de propiedad (amenidades extendidas, ratings)

### Google ADK (Agent Development Kit) — El framework
- **¿Qué es?** Kit de desarrollo de Google para crear agentes de IA.
- **¿Qué hace?** Gestiona la conversación, envía las preguntas a Gemini, ejecuta las herramientas Python y devuelve la respuesta.
- **Herramientas:** Tres funciones Python que hacen queries parametrizadas a BigQuery.

### Google Cloud Platform (GCP) — La infraestructura
- **Proyecto:** `abahanaweb`
- **Región BigQuery:** `EU`
- **Región Vertex AI:** `europe-west1`
- **Servicios usados:** BigQuery, Vertex AI (Gemini), IAM

---

## 3. Arquitectura del sistema

```
┌─────────────────────────────────────────────────────┐
│                    USUARIO                          │
│      "Quiero villa con piscina en Calpe"            │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              GOOGLE ADK (Framework)                 │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │           AGENTE (agent.py)                   │  │
│  │                                               │  │
│  │  Gemini 2.5 Flash decide qué herramienta      │  │
│  │  llamar según la pregunta del usuario         │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │            Herramientas Python               │   │
│  │  listar_propiedades()                        │   │
│  │  buscar_propiedades()                        │   │
│  │  buscar_propiedades_amenidades()             │   │
│  └──────────────────────────────────────────────┘   │
└──────────────────────────┬──────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│         GOOGLE BIGQUERY — capa silver_clean          │
│                                                      │
│  dim_propiedades (891 filas)                         │
│  ├── propiedad_id, nombre, licencia_turismo          │
│  ├── capacidad_pax, total_camas, total_banos         │
│  ├── tiene_piscina_privada, tiene_internet           │
│  ├── pueblo_cercano, zona_nombre, region_nombre      │
│  └── latitud, longitud, fecha_creacion_sistema       │
│                                                      │
│  int_etendo_bookings (891 filas)                     │
│  ├── booking_id, cliente_id, cliente_nombre          │
│  ├── propiedad_nombre + todos los campos de dim      │
│  ├── tiene_aire_salon, tiene_lavadora                │
│  ├── tiene_lavavajillas, admite_animales             │
│  └── score_rating_banos/cocina/interior/exterior     │
└──────────────────────────────────────────────────────┘
```

### Flujo de una consulta

1. **Usuario** escribe: "Villa con lavadora y aire acondicionado en Altea"
2. **ADK** envía la pregunta a **Gemini 2.5 Flash**
3. **Gemini** decide llamar a `buscar_propiedades_amenidades(ubicacion="Altea", lavadora=True, aire_acondicionado=True)`
4. La **función Python** ejecuta SQL parametrizado contra `int_etendo_bookings`
5. **Gemini** recibe los resultados y responde en lenguaje natural
6. **Usuario** recibe la lista de villas o un mensaje indicando que no hay resultados

---

## 4. Estructura del código

```
agente_abahana/
├── .env                 ← Configuración (proyecto GCloud, región, Vertex AI)
├── requirements.txt     ← Dependencias Python
├── agent.py             ← Agente, herramientas y conexión a BigQuery
├── main.py              ← Interfaz de línea de comandos (CLI interactivo)
└── agente_villas/
    └── __init__.py      ← Re-exporta root_agent para compatibilidad con ADK web
```

### agent.py — El corazón del sistema

#### `listar_propiedades()`
Devuelve todas las propiedades del catálogo ordenadas por nombre. Fuente: `dim_propiedades`.

#### `buscar_propiedades(...)`
Búsqueda filtrada contra `dim_propiedades`. Parámetros disponibles:

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `ubicacion` | str | Pueblo cercano (Altea, Calpe, Moraira…) |
| `zona` | str | Zona geográfica |
| `capacidad_min` | int | Personas mínimas |
| `camas_min` | int | Camas mínimas |
| `banos_min` | int | Baños mínimos |
| `piscina` | bool | Piscina privada |
| `internet` | bool | Internet |
| `texto` | str | Búsqueda libre en nombre y tipo de villa |

#### `buscar_propiedades_amenidades(...)`
Búsqueda con amenidades extendidas contra `int_etendo_bookings` (DISTINCT por propiedad). Parámetros adicionales respecto a la búsqueda básica:

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `aire_acondicionado` | bool | Aire acondicionado en salón |
| `lavadora` | bool | Lavadora |
| `lavavajillas` | bool | Lavavajillas |
| `admite_animales` | bool | Mascotas permitidas |

#### `buscar_por_valoracion(...)`
Busca propiedades ordenadas por valoración media (descendente). Fuente: `int_etendo_bookings`.
La valoración media se calcula como media de los 4 scores disponibles: baños, cocina, interior y exterior.

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `rating_min` | float | Puntuación media mínima (escala 1-6) |
| `ubicacion` | str | Pueblo cercano |
| `capacidad_min` | int | Personas mínimas |
| `piscina` | bool | Piscina privada |

> **Nota:** La escala de valoración es 1-6 (media actual del catálogo: 3.4). Valor máximo observado: 6.

#### `obtener_detalle_propiedad(nombre)`
Devuelve la ficha completa de una propiedad buscando por nombre parcial. Fuente: `int_etendo_bookings`.
Incluye todos los campos disponibles: metros, camas por tipo, amenidades, ratings, coordenadas y dirección completa.

**Seguridad en consultas:**
- Queries parametrizadas — sin interpolación de strings del usuario en SQL
- `maximum_bytes_billed = 10 MB` por consulta — evita costes inesperados
- Límite de 20 resultados por consulta (5 en detalle de propiedad)

---

## 5. Cómo ejecutar

```powershell
# 1. Activar entorno virtual
.venv\Scripts\Activate.ps1

# 2. Autenticarse con Google Cloud (credenciales caducan ~12h)
gcloud auth application-default login

# 3a. Lanzar CLI interactivo
python main.py

# 3b. Lanzar interfaz web (recomendado)
adk web
# Abre http://localhost:8000 en el navegador
```

---

## 6. Requisitos para funcionar

- [x] Proyecto GCP `abahanaweb` con facturación activa
- [x] BigQuery habilitado — dataset `silver_clean` con tablas `dim_propiedades` e `int_etendo_bookings`
- [x] Vertex AI API habilitada — modelo `gemini-2.5-flash` accesible
- [x] Credenciales ADC activas (`gcloud auth application-default login`)
- [x] Python 3.13 con entorno virtual `.venv`

### Coste estimado

| Concepto | Precio |
|----------|--------|
| Gemini 2.5 Flash — Input | ~$0.15 / millón de tokens |
| Gemini 2.5 Flash — Output | ~$0.60 / millón de tokens |
| BigQuery — Consultas | $5 por TB (primeros 10 TB/mes gratis) |

Estimación para testing: menos de 1€/mes. Las consultas son pequeñas y la tabla tiene 891 filas.

---

## 7. Limitaciones actuales

- **Sin precios:** Las tablas de `silver_clean` no contienen `precio_noche`. El agente no puede responder preguntas de precio.
- **Sin disponibilidad por fechas:** No hay datos de fechas de entrada/salida en el catálogo.
- **Aire acondicionado escaso:** Solo 33 de 891 propiedades tienen `tiene_aire_salon = TRUE`.
- **Sesión en memoria:** Las conversaciones no persisten entre reinicios del servidor.

---

## 8. Perspectivas futuras

### Medio plazo

#### Datos
- **Precios:** Incorporar `precio_noche` a las tablas silver cuando esté disponible en Etendo. Permitirá filtros `precio_max` / `precio_min` y ordenar por precio.
- **Disponibilidad por fechas:** Añadir fechas de entrada/salida a los bookings para que el agente pueda responder "¿está libre del 15 al 22 de agosto?".
- **Fotos/URLs:** Enriquecer las respuestas con enlaces a imágenes de cada villa.

#### Herramientas del agente
- **Comparar villas:** Función que recibe dos nombres y devuelve comparativa de amenidades, capacidad y ratings lado a lado.
- **Búsqueda por zona geográfica avanzada:** Filtrar por distancia a playa o coordenadas, aprovechando `latitud` y `longitud` ya disponibles.

#### Infraestructura
- **Multi-idioma:** Gemini es multilingüe — añadir detección de idioma a la instrucción para que el agente responda en inglés, francés o alemán según el usuario.

---

### Largo plazo

#### Infraestructura
- **Persistencia de sesión:** Sustituir `InMemorySessionService` por `DatabaseSessionService` (Cloud Spanner o Firestore). Las conversaciones actuales se pierden al reiniciar el servidor.
- **Frontend custom:** Sustituir `adk web` por una interfaz integrada en la web de Abahana (widget de chat embebido).

#### Integración con Etendo
- **Consulta de disponibilidad real:** Conectar con Etendo vía API para verificar disponibilidad en tiempo real en lugar de depender solo de los datos de BigQuery.
- **Inicio de reservas:** Permitir al agente iniciar o pre-rellenar una reserva directamente desde el chat, integrando con el flujo de reservas de Etendo.
- **Sincronización de precios:** Cuando Etendo exponga precios por noche en el data warehouse, actualizar la capa silver y añadir la herramienta de búsqueda por precio.

---

## 9. Notas técnicas

- **Autenticación:** Application Default Credentials (ADC). Las credenciales caducan ~12h — ejecutar `gcloud auth application-default login` para renovar.
- **Por qué dos tablas:** `dim_propiedades` tiene 1 fila por propiedad (limpia para catálogo). `int_etendo_bookings` tiene amenidades extendidas (aire, lavadora, animales, ratings) que `dim_` no incluye — se usa con `DISTINCT` para evitar duplicados.
- **Python:** 3.13 con entorno virtual `.venv`.
- **Dependencias principales:** `google-adk[bigquery]`, `google-cloud-bigquery`, `google-genai`, `python-dotenv`.
