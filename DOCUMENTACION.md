# Agente IA — Abahana Villas
## Asistente virtual para consultas de villas vacacionales

---

## 1. ¿Qué es este proyecto?

Un **asistente virtual inteligente** que responde preguntas en lenguaje natural sobre las villas de alquiler vacacional de Abahana Villas. El usuario escribe preguntas como si hablara con una persona, y el agente consulta la base de datos real y la web corporativa y responde con información actualizada.

### Ejemplos de uso

```
Usuario: "¿Qué villas tienen piscina en Calpe para 6 personas?"
Agente:  "He encontrado 20 villas en Calpe con piscina para 6 o más personas:
          - 1005 ALFALS | Calpe | 6 personas | Piscina: Sí ..."

Usuario: "¿Dónde están las oficinas de Abahana?"
Agente:  "Abahana Villas tiene dos oficinas de check-in:
          Oficina de Calpe: Avda. Jaime I El Conquistador, nº31. +34 965 838 233
          Oficina de Moraira: C.C. Moravit. Ctra. Moraira-Calpe, nº306. +34 965 595 615"

Usuario: "¿Cuál es la política de privacidad?"
Agente:  [Accede a la web en tiempo real y resume el contenido de esa página]
```

**Estado actual: funcional con datos reales.** 891 propiedades desde la capa Silver de BigQuery + acceso en tiempo real a la web corporativa.

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

### Playwright — Navegador headless para la web
- **¿Qué es?** Biblioteca que controla un navegador Chromium real sin interfaz gráfica.
- **¿Para qué?** La web de Abahana es una SPA (Single Page Application) que renderiza su contenido con JavaScript. `requests` normal solo ve el HTML vacío. Playwright ejecuta el JS y extrae el contenido real.

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
│      "¿Dónde están las oficinas?"                   │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              GOOGLE ADK (Framework)                 │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │      AGENTE (agent.py) — 3 roles              │  │
│  │  cliente / interno / admin                    │  │
│  │                                               │  │
│  │  Gemini 2.5 Flash decide qué herramienta      │  │
│  │  llamar según la pregunta del usuario         │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │            Herramientas Python               │   │
│  │  listar_propiedades()         → BigQuery     │   │
│  │  buscar_propiedades()         → BigQuery     │   │
│  │  buscar_por_valoracion()      → BigQuery     │   │
│  │  obtener_detalle_propiedad()  → BigQuery     │   │
│  │  consultar_web()              → Web real     │   │
│  └──────────────────────────────────────────────┘   │
└────────────┬─────────────────────────┬──────────────┘
             │                         │
             ▼                         ▼
┌─────────────────────┐   ┌────────────────────────────┐
│   GOOGLE BIGQUERY   │   │  WEB CORPORATIVA           │
│   silver_clean      │   │  abahanavillas.com         │
│                     │   │                            │
│  dim_propiedades    │   │  Playwright (Chromium)     │
│  int_etendo_bookings│   │  renderiza JS → extrae     │
│                     │   │  texto limpio              │
└─────────────────────┘   └────────────────────────────┘
```

### Flujo de una consulta de villa

1. **Usuario** escribe: "Villa con lavadora y aire acondicionado en Altea"
2. **ADK** envía la pregunta a **Gemini 2.5 Flash**
3. **Gemini** decide llamar a `buscar_propiedades(ubicacion="Altea", lavadora=True, aire_acondicionado=True)`
4. La **función Python** ejecuta SQL parametrizado contra `int_etendo_bookings`
5. **Gemini** recibe los resultados y responde en lenguaje natural

### Flujo de una consulta de la web

1. **Usuario** escribe: "¿Dónde están las oficinas?"
2. **ADK** envía la pregunta a **Gemini 2.5 Flash**
3. **Gemini** decide llamar a `consultar_web("https://www.abahanavillas.com/es/contacto/")`
4. **Playwright** lanza Chromium, carga la página, espera a que renderice el JS, extrae el HTML
5. **BeautifulSoup** limpia el HTML (quita nav, footer, scripts, imágenes, formularios, etc.)
6. **Gemini** recibe el texto limpio y responde en lenguaje natural

---

## 4. Sistema de roles

El agente tiene tres variantes según el tipo de usuario. Cada una tiene acceso a distintas herramientas:

| Herramienta | cliente | interno | admin |
|---|:---:|:---:|:---:|
| `listar_propiedades` | ✓ | ✓ | ✓ |
| `buscar_propiedades` | ✓ | ✓ | ✓ |
| `buscar_por_valoracion` | ✓ | ✓ | ✓ |
| `consultar_web` | ✓ | ✓ | ✓ |
| `obtener_detalle_propiedad` | ✗ | ✓ | ✓ |

- **cliente** — acceso público: catálogo, búsqueda, valoraciones, web corporativa.
- **interno** — agentes de ventas: incluye ficha completa con dirección, coordenadas y ratings desglosados.
- **admin** — administración: igual que interno, preparado para herramientas sensibles futuras.

```powershell
python main.py --rol cliente   # por defecto
python main.py --rol interno
python main.py --rol admin
```

---

## 5. Estructura del código

```
agente_abahana/
├── .env                 ← Configuración (proyecto GCloud, región, Vertex AI)
├── requirements.txt     ← Dependencias Python
├── agent.py             ← Agente, herramientas, BigQuery y scraping web
├── main.py              ← Interfaz de línea de comandos (CLI interactivo)
└── agente_villas/
    └── __init__.py      ← Re-exporta root_agent para compatibilidad con ADK web
```

---

## 6. Herramientas del agente

### `listar_propiedades()`
Devuelve todas las propiedades del catálogo ordenadas por nombre. Fuente: `dim_propiedades`.

### `buscar_propiedades(...)`
Búsqueda filtrada con cualquier combinación de filtros. Fuente: `int_etendo_bookings`.

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `ubicacion` | str | Pueblo cercano (Altea, Calpe, Moraira…) |
| `zona` | str | Zona geográfica (Costa Blanca Norte…) |
| `capacidad_min` | int | Personas mínimas |
| `camas_min` | int | Camas mínimas |
| `banos_min` | int | Baños mínimos |
| `metros_habitables_min` | int | Metros habitables mínimos |
| `piscina` | bool | Piscina privada |
| `internet` | bool | Internet |
| `aire_acondicionado` | bool | Aire acondicionado en salón |
| `lavadora` | bool | Lavadora |
| `lavavajillas` | bool | Lavavajillas |
| `admite_animales` | bool | Mascotas permitidas |
| `texto` | str | Búsqueda libre en nombre y tipo de villa |

### `buscar_por_valoracion(...)`
Busca propiedades ordenadas por valoración media (escala 1-6). Fuente: `int_etendo_bookings`.
La media se calcula sobre los 4 scores disponibles: baños, cocina, interior y exterior.

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `rating_min` | float | Puntuación media mínima (escala 1-6) |
| `ubicacion` | str | Pueblo cercano |
| `capacidad_min` | int | Personas mínimas |
| `piscina` | bool | Piscina privada |

### `obtener_detalle_propiedad(nombre)` *(solo interno y admin)*
Ficha completa de una propiedad por nombre parcial. Incluye dirección, coordenadas, metros, desglose de camas y ratings por categoría. Fuente: `int_etendo_bookings`.

### `consultar_web(url)`
Accede a cualquier página de `abahanavillas.com` en tiempo real y devuelve texto limpio + enlaces de la página.

- **Cómo funciona:** Playwright lanza un Chromium en segundo plano, carga la URL, espera 3s a que el JS renderice, extrae el HTML.
- **Limpieza:** BeautifulSoup elimina scripts, estilos, nav, imágenes, vídeos, SVGs, selects y opciones. Filtra líneas duplicadas y menores de 10 caracteres.
- **Enlaces:** BeautifulSoup extrae todos los `<a href>` del dominio con su texto y URL real. El agente recibe las URLs exactas de cada sección (política de privacidad, contacto, etc.) sin necesidad de inferirlas. Máximo 50 enlaces por página.
- **Por qué extraer enlaces:** BeautifulSoup extrae solo texto, no atributos `href`. Sin esta extracción explícita el agente tendría que adivinar las URLs de las páginas internas.
- **Retorno:** `{ titulo, contenido (máx. 9.000 chars), enlaces (máx. 50), url }`
- **Seguridad:** Solo permite URLs del dominio `abahanavillas.com`.
- **Restricción técnica:** Playwright es async pero las tools de ADK corren en un event loop ya activo. Se resuelve lanzando Playwright en un hilo separado con su propio event loop.

**Seguridad en consultas SQL:**
- Queries parametrizadas — sin interpolación de strings del usuario en SQL
- `maximum_bytes_billed = 10 MB` por consulta — evita costes inesperados
- Límite de 20 resultados por consulta (5 en detalle de propiedad)

---

## 7. Cómo ejecutar

```powershell
# 1. Activar entorno virtual
.venv\Scripts\Activate.ps1

# 2. Autenticarse con Google Cloud (credenciales caducan ~12h)
gcloud auth application-default login

# 3a. Lanzar CLI interactivo
python main.py

# 3b. Con rol específico
python main.py --rol interno

# 3c. Lanzar interfaz web ADK (recomendado para pruebas)
adk web
# Abre http://localhost:8000 en el navegador
```

---

## 8. Requisitos para funcionar

- [x] Proyecto GCP `abahanaweb` con facturación activa
- [x] BigQuery habilitado — dataset `silver_clean` con tablas `dim_propiedades` e `int_etendo_bookings`
- [x] Vertex AI API habilitada — modelo `gemini-2.5-flash` accesible
- [x] Credenciales ADC activas (`gcloud auth application-default login`)
- [x] Python 3.13 con entorno virtual `.venv`
- [x] Chromium descargado: `playwright install chromium` (se instala en `AppData\Local\ms-playwright`)

### Coste estimado

| Concepto | Precio |
|----------|--------|
| Gemini 2.5 Flash — Input | ~$0.15 / millón de tokens |
| Gemini 2.5 Flash — Output | ~$0.60 / millón de tokens |
| BigQuery — Consultas | $5 por TB (primeros 10 TB/mes gratis) |

Estimación para testing: menos de 1€/mes. Las consultas son pequeñas y la tabla tiene 891 filas.
Consultas web con Playwright consumen más tokens de Gemini (~2.000-5.000 tokens por página). Vigilar si el uso es intensivo.

---

## 9. Limitaciones actuales

- **Sin precios:** Las tablas de `silver_clean` no contienen `precio_noche`. El agente no puede responder preguntas de precio.
- **Sin disponibilidad por fechas:** No hay datos de fechas de entrada/salida en el catálogo.
- **Aire acondicionado escaso:** Solo 33 de 891 propiedades tienen `tiene_aire_salon = TRUE`.
- **Sesión en memoria:** Las conversaciones no persisten entre reinicios del servidor.
- **Playwright lento:** Cada consulta web tarda ~5-8 segundos (Chromium lanza, renderiza, cierra). No apto para uso con alta concurrencia.
- **Web dinámica:** Si la web cambia de estructura o añade protección anti-bot, el scraping puede romperse.

---

## 10. Perspectivas futuras

### Medio plazo

#### Datos
- **Precios:** Incorporar `precio_noche` a las tablas silver. Permitirá filtros `precio_max`/`precio_min`.
- **Disponibilidad por fechas:** Añadir fechas de entrada/salida para que el agente pueda responder "¿está libre del 15 al 22 de agosto?".
- **Fotos/URLs:** Enriquecer respuestas con enlaces a imágenes de cada villa.

#### Herramientas del agente
- **Comparar villas:** Función que recibe dos nombres y devuelve comparativa de amenidades, capacidad y ratings.
- **Búsqueda geográfica avanzada:** Filtrar por distancia a playa usando `latitud` y `longitud` ya disponibles.

#### Web scraping
- **Caché de páginas estáticas:** Para páginas que no cambian (política de privacidad, aviso legal), guardar el texto en BigQuery y refrescar semanalmente. Evita lanzar Playwright para contenido estático.
- **Instancia Playwright persistente:** Mantener el browser abierto entre llamadas en lugar de crear y destruir Chromium en cada consulta. Mejora la latencia de ~5-8s a ~1-2s.

### Largo plazo

#### Infraestructura
- **Persistencia de sesión:** Sustituir `InMemorySessionService` por `DatabaseSessionService` (Cloud Spanner o Firestore).
- **Frontend custom:** Sustituir `adk web` por un widget de chat embebido en la web de Abahana.
- **Multi-idioma:** Añadir detección de idioma para que el agente responda en inglés, francés o alemán.

#### Integración con Etendo
- **Disponibilidad real:** Conectar con Etendo vía API para verificar disponibilidad en tiempo real.
- **Inicio de reservas:** Permitir al agente pre-rellenar o iniciar una reserva desde el chat.

---

## 11. Notas técnicas

- **Autenticación:** Application Default Credentials (ADC). Caducan ~12h — ejecutar `gcloud auth application-default login` para renovar.
- **Por qué dos tablas:** `dim_propiedades` tiene 1 fila por propiedad (catálogo limpio). `int_etendo_bookings` tiene amenidades extendidas (aire, lavadora, animales, ratings) que `dim_` no incluye — se usa con `DISTINCT` para evitar duplicados.
- **Por qué Playwright y no `requests`:** La web de Abahana es una SPA. El HTML estático solo contiene ~369 caracteres de contenido real. Todo lo demás lo inyecta JavaScript. `requests` no ejecuta JS.
- **Por qué un hilo separado para Playwright:** Las tools de ADK se ejecutan dentro de un event loop de asyncio ya activo. `asyncio.run()` no puede anidarse en un loop existente. La solución es crear un thread separado con su propio event loop donde corre Playwright.
- **Python:** 3.13 con entorno virtual `.venv`.
- **Dependencias principales:** `google-adk[bigquery]`, `google-cloud-bigquery`, `google-genai`, `python-dotenv`, `playwright`, `beautifulsoup4`, `requests`.
