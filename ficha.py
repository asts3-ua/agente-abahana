"""Ficha completa de una villa, con el diseño de la web de Abahana.

Sale de los datos (agent.ficha_completa), no del texto del modelo. Se pinta
con st.html, que mete el HTML en la página: todo el CSS va bajo .abv-ficha
para no tocar el resto de la app. Sin JavaScript: el bloque de acceso y
seguridad se pliega con <details>.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any
from urllib.parse import urlparse

import requests

MAXIMO_BOTONES = 8
MAXIMO_FOTOS = 4

# ---------------------------------------------------------------------------
# Qué villas salen en una respuesta
# ---------------------------------------------------------------------------


def _nombres(valor: Any, encontrados: list[str]) -> None:
    if isinstance(valor, dict):
        for clave in ("villa_nombre", "nombre"):
            nombre = valor.get(clave)
            if isinstance(nombre, str) and nombre.strip():
                encontrados.append(nombre.strip())
                break
    elif isinstance(valor, list):
        for elemento in valor:
            if isinstance(elemento, dict):
                _nombres(elemento, encontrados)


def villas_del_turno(herramientas: list[tuple[str, dict, Any]]) -> list[str]:
    """Nombres de las villas que devolvieron las herramientas, sin repetir y
    en el orden en que salieron. Solo las de listas y fichas: un nombre suelto
    en los argumentos no es una villa encontrada."""
    encontrados: list[str] = []
    for _nombre, _args, respuesta in herramientas:
        if not isinstance(respuesta, dict):
            continue
        for valor in respuesta.values():
            if isinstance(valor, list):
                _nombres(valor, encontrados)
    vistos: dict[str, str] = {}
    for nombre in encontrados:
        vistos.setdefault(nombre.upper(), nombre)
    return list(vistos.values())[:MAXIMO_BOTONES]


# ---------------------------------------------------------------------------
# Fotos: no están en nuestros datos, se toman de la página de la villa
# ---------------------------------------------------------------------------

# Las fotos se llaman 1341_01.webp o 1395-01.webp: referencia y número.
_FOTO = re.compile(r"""(?:https://www\.abahanavillas\.com)?(/documents/\d+/\d+/(\d+)[_-](\d+)\.(?:webp|jpe?g|png)/[^"'\s<>]+)""")


def _es_de_abahana(url: str) -> bool:
    try:
        partes = urlparse(url)
    except ValueError:
        return False
    host = (partes.hostname or "").lower()
    return partes.scheme == "https" and (host == "abahanavillas.com" or host.endswith(".abahanavillas.com"))


def fotos_de_la_web(url: str | None) -> list[str]:
    """Las primeras fotos de la galería de la villa en la web. Si la web no
    responde, la ficha sale sin fotos."""
    if not url or not _es_de_abahana(url):
        return []
    try:
        pagina = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0 abahana-agente"})
        if pagina.status_code != 200:
            return []
        texto = pagina.text
    except Exception:
        return []
    fotos: dict[int, str] = {}
    for m in _FOTO.finditer(texto):
        ruta = _html.unescape(m.group(1)).replace("&download=true", "")
        fotos.setdefault(int(m.group(3)), "https://www.abahanavillas.com" + ruta)
    return [fotos[i] for i in sorted(fotos)][:MAXIMO_FOTOS]


def referencia(fotos: list[str]) -> str | None:
    """La referencia de la villa (1341) es el prefijo de sus fotos."""
    for foto in fotos:
        m = _FOTO.search(foto)
        if m:
            return m.group(2)
    return None


# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------


def _e(valor: Any) -> str:
    return _html.escape(str(valor), quote=True)


def _vacio(valor: Any) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def _num(valor: Any) -> str:
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    if n.is_integer():
        return f"{int(n):,}".replace(",", ".")
    return f"{n:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _distancia(metros: Any) -> str:
    m = float(metros)
    if m >= 1000:
        km = m / 1000
        return f"{_num(round(km)) if km >= 20 else _num(round(km, 1))} km"
    return f"{_num(m)} m"


def _fecha(valor: Any) -> str:
    texto = str(valor)[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        a, m, d = texto.split("-")
        return f"{d}/{m}/{a}"
    return str(valor)


def _sin_repetir(*valores: Any) -> list[str]:
    vistos: dict[str, str] = {}
    for v in valores:
        if not _vacio(v):
            vistos.setdefault(str(v).strip().upper(), str(v).strip())
    return list(vistos.values())


def _positivo(valor: Any) -> bool:
    try:
        return float(valor) > 0
    except (TypeError, ValueError):
        return False


class _Lector:
    """Da los datos de la villa y apunta cuáles se han usado: lo que sobre va
    a "Otros datos", para que ninguno se quede fuera de la ficha."""

    _NO_SE_MUESTRAN = {
        "villa_id", "latitud", "longitud", "plantas", "url_web",
        "licencia_vut_normalizada", "enlace_etendo", "detalle_banios_disponible",
        "detalle_dormitorios_disponible", "banios_con_detalle",
    }

    def __init__(self, villa: dict[str, Any]):
        self.villa = villa
        self.usados: set[str] = set(self._NO_SE_MUESTRAN)

    def __call__(self, clave: str) -> Any:
        self.usados.add(clave)
        return self.villa.get(clave)

    def todos(self, *claves: str) -> list[Any]:
        """Varios datos a la vez; así un `or` no deja ninguno sin marcar."""
        return [self(c) for c in claves]

    def sobrantes(self) -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self.villa.items()
                if k not in self.usados and not _vacio(v) and v is not False]


# Una línea de un bloque: (clase, texto) o ("fila", etiqueta, valor).
Linea = tuple


def _si(texto: str) -> Linea:
    return ("si", texto)


def _no(texto: str) -> Linea:
    return ("no", texto)


def _nc(texto: str) -> Linea:
    return ("nc", f"{texto}: no consta")


def _dato(texto: str) -> Linea:
    return ("dato", texto)


def _fila(etiqueta: str, valor: str) -> Linea:
    return ("fila", etiqueta, valor)


def _plural(n: Any, uno: str, varios: str) -> str:
    return f"{_num(n)} {uno if str(_num(n)) == '1' else varios}"


# ---------------------------------------------------------------------------
# Bloques
# ---------------------------------------------------------------------------


def _general(g: _Lector, ref: str | None) -> list[Linea]:
    lineas: list[Linea] = []
    if ref:
        lineas.append(_dato(f"Referencia: {ref}"))
    for clave in ("tipovilla_nombre_comercial", "tipovilla_descripcion"):
        if not _vacio(g(clave)):
            lineas.append(_dato(str(g(clave))))
    if not _vacio(g("anio_construccion")):
        lineas.append(_si(f"Año de construcción: {g('anio_construccion')}"))
    reformada, anyo = g.todos("esta_reformada", "anyo_reforma")
    if reformada:
        lineas.append(_si("Reformada" + (f" en {anyo}" if not _vacio(anyo) else "")))
    if _positivo(g("capacidad_pax")):
        lineas.append(_si(f"Capacidad: {_plural(g('capacidad_pax'), 'persona', 'personas')}"))
    if _positivo(g("numero_plantas")):
        lineas.append(_si(_plural(g("numero_plantas"), "planta", "plantas")))
    metros = [f"{_num(g('m2_habitables'))} m² habitables" if _positivo(g("m2_habitables")) else "",
              f"parcela de {_num(g('m2_parcela'))} m²" if _positivo(g("m2_parcela")) else ""]
    if any(metros):
        lineas.append(_si(" · ".join(m for m in metros if m)))
    animales = g("admite_animales")
    if animales is True:
        lineas.append(_si("Se admiten mascotas"))
    elif animales is False:
        lineas.append(_no("No se admiten mascotas"))
    return lineas


def _localizacion(g: _Lector) -> list[Linea]:
    lineas = [_si(t) for c, t in (
        ("zona_tranquila", "Zona tranquila"), ("zona_aislada", "Zona aislada"),
        ("zona_campo", "Zona de campo"), ("urbanizacion_residencial", "Urbanización residencial"),
        ("cerca_del_mar", "Cerca del mar"),
    ) if g(c)]
    lugar = " · ".join(str(x) for x in (g("zona"), g("region")) if not _vacio(x))
    if lugar:
        lineas.append(_dato(lugar))
    if not _vacio(g("direccion")):
        direccion = re.sub(r"(\s*-\s*)+", " - ", str(g("direccion"))).strip(" -")
        lineas.append(_dato(direccion))
    return lineas


def _vistas(g: _Lector) -> list[Linea]:
    return [_si(t) for c, t in (
        ("tiene_vista_mar", "Vista al mar"), ("tiene_vista_panoramica_mar", "Vista panorámica al mar"),
        ("tiene_vista_despejada", "Vista despejada"), ("tiene_vista_montana", "Vista a la montaña"),
        ("tiene_vista_campo", "Vista al campo"), ("tiene_vista_limitada", "Vistas limitadas"),
    ) if g(c)]


def _piscina(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    privada, comun = g("tiene_piscina_privada"), g("tiene_piscina_comun")
    if privada:
        medidas = (f" · {_num(g('piscina_ancho_m'))} × {_num(g('piscina_largo_m'))} m"
                   if _positivo(g("piscina_largo_m")) and _positivo(g("piscina_ancho_m")) else "")
        lineas.append(_si(f"Piscina privada{medidas}"))
    g("piscina_largo_m"), g("piscina_ancho_m")
    pmin, pmax = g("piscina_profundidad_min_m"), g("piscina_profundidad_max_m")
    if _positivo(pmin) and _positivo(pmax):
        lineas.append(_si(f"Profundidad de {_num(pmin)} a {_num(pmax)} m"))
    elif _positivo(pmax):
        lineas.append(_si(f"Profundidad máxima {_num(pmax)} m"))
    if comun:
        lineas.append(_si("Piscina comunitaria"))
    if privada or comun:
        climatizada = g("piscina_climatizada")
        if climatizada is True:
            lineas.append(_si("Climatizada"))
        elif climatizada is False:
            lineas.append(_no("No climatizada"))
    else:
        g("piscina_climatizada")
        lineas.append(_no("Sin piscina"))
    return lineas


def _habitaciones(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    if _positivo(g("dormitorios")):
        suite = f" · {_num(g('dormitorios_en_suite'))} en suite" if _positivo(g("dormitorios_en_suite")) else ""
        lineas.append(_si(_plural(g("dormitorios"), "dormitorio", "dormitorios") + suite))
    g("dormitorios_en_suite")
    camas = [(c, u, v) for c, u, v in (
        ("camas_king_size", "king size", "king size"), ("camas_dobles", "doble", "dobles"),
        ("camas_simples", "individual", "individuales"), ("camas_partidas", "partida", "partidas"),
        ("literas", "litera", "literas"), ("camas_nido", "nido", "nido"),
    )]
    partes = [_plural(g(c), u, v) for c, u, v in camas if _positivo(g(c))]
    if _positivo(g("camas_totales")):
        lineas.append(_si(_plural(g("camas_totales"), "cama", "camas") + (f": {', '.join(partes)}" if partes else "")))
    if _positivo(g("estancias_con_sofacama")):
        lineas.append(_si(_plural(g("estancias_con_sofacama"), "estancia con sofá cama", "estancias con sofá cama")))
    banos = []
    if _positivo(g("numero_banos")):
        banos.append(_plural(g("numero_banos"), "baño", "baños"))
    if _positivo(g("numero_aseos")):
        banos.append(_plural(g("numero_aseos"), "aseo", "aseos"))
    if banos:
        lineas.append(_si(" + ".join(banos)))
    detalle = [f"{_num(g(c))} {t}" for c, t in (
        ("banios_ensuite", "en suite"), ("banios_con_banera", "con bañera"),
        ("banios_con_ducha", "con ducha"), ("banios_con_jacuzzi", "con jacuzzi"),
        ("banios_con_bide", "con bidé"),
    ) if _positivo(g(c))]
    if detalle:
        lineas.append(_dato("Baños: " + ", ".join(detalle)))
    # Los de la ficha técnica repiten el desglose por planta con otro recuento.
    for c in ("banos_con_banera", "banos_con_ducha", "banos_en_suite"):
        g(c)
    for c, u, v in (("salones", "salón", "salones"), ("salones_comedor", "salón comedor", "salones comedor"),
                    ("comedores", "comedor", "comedores")):
        if _positivo(g(c)):
            lineas.append(_si(_plural(g(c), u, v)))
    anexo, anexos = g.todos("tiene_anexo", "num_anexos")
    if anexo or _positivo(anexos):
        lineas.append(_si(_plural(anexos, "anexo", "anexos") if _positivo(anexos) else "Anexo"))
    return lineas


def _exterior(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    tipo = g("tipo_parcela") or g("tipo_parcela_ficha")
    lineas.append(_si(f"Parcela {str(tipo).lower()}") if not _vacio(tipo)
                  else _nc("Parcela (vallada o abierta)"))
    for c in ("parcela_cerrada", "parcela_semicerrada", "parcela_abierta"):
        g(c)
    terreno = g("terreno_parcela") or g("terreno_parcela_ficha")
    lineas.append(_si(f"Terreno {str(terreno).lower()}") if not _vacio(terreno)
                  else _nc("Terreno de la parcela"))
    jardin = [t for c, t in (("tiene_jardin", "Jardín"), ("tiene_cesped", "césped"),
                             ("tiene_arbolado", "arbolado")) if g(c)]
    if jardin:
        lineas.append(_si(" · ".join([jardin[0]] + jardin[1:]).capitalize()))
    terrazas = [t for c, t in (("tiene_terraza_cubierta", "cubierta"),
                               ("tiene_terraza_descubierta", "descubierta")) if g(c)]
    if terrazas:
        lineas.append(_si("Terraza " + " y ".join(terrazas)))
    for c, t in (("tiene_solarium", "Solárium"), ("tiene_ducha_exterior", "Ducha exterior")):
        if g(c):
            lineas.append(_si(t))
    if g("tiene_barbacoa"):
        clases = [t for c, t in (("tiene_barbacoa_obra", "de obra"), ("tiene_barbacoa_portatil", "portátil")) if g(c)]
        texto = "Barbacoa " + " y ".join(clases) if clases else "Barbacoa"
        extras = []
        if not _vacio(g("tipo_barbacoa_portatil")):
            extras.append(f"portátil de {str(g('tipo_barbacoa_portatil')).lower()}")
        if not _vacio(g("gas_barbacoa")):
            extras.append(f"gas: {str(g('gas_barbacoa')).lower()}")
        if not _vacio(g("barbacoa_plancha")):
            extras.append(f"plancha {str(g('barbacoa_plancha')).lower()}")
        lineas.append(_si(texto + (" · " + " · ".join(extras) if extras else "")))
    elif g("tiene_barbacoa") is False:
        lineas.append(_no("Sin barbacoa"))
    for c in ("tiene_barbacoa_obra", "tiene_barbacoa_portatil", "tipo_barbacoa_portatil",
              "gas_barbacoa", "barbacoa_plancha"):
        g(c)
    return lineas


def _parking(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    for tiene, plazas, texto in (
        ("tiene_garaje", "plazas_garaje", "Garaje"),
        ("tiene_parking_cubierto", "plazas_cubiertas", "Parking cubierto"),
        ("tiene_parking_descubierto", "plazas_descubiertas", "Parking descubierto"),
        ("tiene_parking_calle", "plazas_calle", "Aparcamiento en la calle"),
    ):
        hay, n = g.todos(tiene, plazas)
        if hay or _positivo(n):
            lineas.append(_si(texto + (f" · {_plural(n, 'plaza', 'plazas')}" if _positivo(n) else "")))
    return lineas or [_no("Sin parking")]


def _cocina(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    for c, u, v in (("cocinas_separadas", "cocina independiente", "cocinas independientes"),
                    ("cocinas_americanas", "cocina americana", "cocinas americanas"),
                    ("cocinas_exteriores", "cocina exterior", "cocinas exteriores")):
        if _positivo(g(c)):
            lineas.append(_si(_plural(g(c), u, v)))
    placas = [t for c, t in (("tiene_induccion", "inducción"), ("tiene_vitroceramica", "vitrocerámica"),
                             ("tiene_placa_gas", "gas"), ("tiene_placa_electrica", "eléctrica")) if g(c)]
    if placas:
        lineas.append(_si("Placa de " + " y ".join(placas)))
    aparatos = [t for c, t in (("tiene_horno", "horno"), ("tiene_microondas", "microondas"),
                               ("tiene_frigorifico", "frigorífico"), ("tiene_congelador", "congelador")) if g(c)]
    if aparatos:
        lineas.append(_si(" · ".join(aparatos).capitalize()))
    lavavajillas = g("tiene_lavavajillas")
    if lavavajillas is True:
        lineas.append(_si("Lavavajillas"))
    elif lavavajillas is False:
        lineas.append(_no("Sin lavavajillas"))
    if g("tiene_gas"):
        lineas.append(_si("Gas"))
    if not _vacio(g("tipo_cafetera")):
        lineas.append(_si(f"Cafetera {str(g('tipo_cafetera')).lower()}"))
    return lineas


def _clima(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    aire = g("tiene_aire_acondicionado")
    equipos = g("equipos_aire_acondicionado")
    if aire or _positivo(equipos):
        lineas.append(_si("Aire acondicionado" + (f" · {_plural(equipos, 'equipo', 'equipos')}" if _positivo(equipos) else "")))
    elif aire is False:
        lineas.append(_no("Sin aire acondicionado"))
    if _positivo(g("aire_dormitorios")):
        lineas.append(_si(f"Aire en {_plural(g('aire_dormitorios'), 'dormitorio', 'dormitorios')}"))
    if _positivo(g("equipos_aire_frio")):
        lineas.append(_dato(f"Frío: {_plural(g('equipos_aire_frio'), 'equipo', 'equipos')}"))
    if _positivo(g("equipos_aire_calor")):
        lineas.append(_dato(f"Calor: {_plural(g('equipos_aire_calor'), 'equipo', 'equipos')}"))
    for c, t in (("tiene_bomba_calor", "Bomba de calor"), ("tiene_suelo_radiante", "Suelo radiante"),
                 ("tiene_radiadores", "Radiadores")):
        if g(c):
            lineas.append(_si(t))
    chimenea, chimeneas = g.todos("tiene_chimenea", "num_chimeneas")
    if chimenea or _positivo(chimeneas):
        lineas.append(_si(_plural(chimeneas, "chimenea", "chimeneas") if _positivo(chimeneas) else "Chimenea"))
    elif chimenea is False:
        lineas.append(_no("Sin chimenea"))
    for c, u, v in (("ventiladores_techo", "ventilador de techo", "ventiladores de techo"),
                    ("ventiladores_pie", "ventilador de pie", "ventiladores de pie")):
        if _positivo(g(c)):
            lineas.append(_si(_plural(g(c), u, v)))
    return lineas


def _equipamiento(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    internet = g("tiene_internet")
    if internet is True:
        lineas.append(_si("Internet"))
    elif internet is False:
        lineas.append(_no("Sin internet"))
    amplificador, amplificadores, donde = g.todos(
        "tiene_amplificador_wifi", "amplificadores_wifi", "amplificadores_wifi_ubicacion")
    if amplificador or _positivo(amplificadores):
        lineas.append(_si("Amplificador wifi" + (f" ({donde})" if not _vacio(donde) else "")))
    lavadoras, lavadora = g.todos("num_lavadoras", "tiene_lavadora")
    if _positivo(lavadoras) or lavadora:
        lineas.append(_si(_plural(lavadoras, "lavadora", "lavadoras") if _positivo(lavadoras) else "Lavadora"))
    secadora, secadoras = g.todos("tiene_secadora", "num_secadoras")
    if secadora or _positivo(secadoras):
        lineas.append(_si(_plural(secadoras, "secadora", "secadoras") if _positivo(secadoras) else "Secadora"))
    elif secadora is False:
        lineas.append(_no("Sin secadora"))
    if g("tiene_plancha"):
        lineas.append(_si("Plancha"))
    if _positivo(g("num_mosquiteras")):
        lineas.append(_si(_plural(g("num_mosquiteras"), "mosquitera", "mosquiteras")))
    tv = [t for c, t in (("tiene_smart_tv", "Smart TV"), ("tiene_tdt", "TDT"),
                         ("tiene_tv_satelite", "satélite"), ("tiene_dvd", "DVD")) if g(c)]
    if g("tiene_tv") is True or tv:
        lineas.append(_si(" · ".join(tv) if tv else "Televisión"))
    if not _vacio(g("tv_satelite")) and str(g("tv_satelite")).lower() != "no":
        lineas.append(_dato(f"Satélite: {g('tv_satelite')}"))
    if g("tiene_altavoz"):
        desc = g("altavoz_descripcion")
        lineas.append(_si("Altavoz" + (f" ({desc})" if not _vacio(desc) else "")))
    g("altavoz_descripcion")
    return lineas


_OCIO = (("tiene_gimnasio", "Gimnasio"), ("tiene_sauna", "Sauna"), ("tiene_jacuzzi", "Jacuzzi"),
         ("tiene_hidromasaje", "Hidromasaje"), ("tiene_billar", "Billar"), ("tiene_pingpong", "Ping-pong"),
         ("tiene_pista_tenis", "Pista de tenis"), ("tiene_pista_padel", "Pista de pádel"),
         ("tiene_petanca", "Petanca"), ("tiene_columpio", "Columpio"), ("tiene_tobogan", "Tobogán"),
         ("tiene_futbolin", "Futbolín"), ("tiene_cama_elastica", "Cama elástica"))


def _ocio(g: _Lector) -> list[Linea]:
    return [_si(t) for c, t in _OCIO if g(c)] or [_dato("Sin extras de ocio registrados")]


_DISTANCIAS = (
    ("distancia_mar_m", "Mar"), ("distancia_playa_arena_m", "Playa de arena"),
    ("distancia_cala_m", "Cala"), ("distancia_restaurantes_m", "Restaurantes"),
    ("distancia_supermercado_m", "Supermercado"), ("distancia_pueblo_m", "Pueblo"),
    ("distancia_centro_medico_m", "Centro médico"), ("distancia_banco_m", "Banco"),
    ("distancia_tenis_m", "Tenis"), ("distancia_club_nautico_m", "Club náutico"),
    ("distancia_golf_m", "Golf"), ("distancia_hipica_m", "Hípica"),
    ("distancia_alicante_m", "Alicante"), ("distancia_valencia_m", "Valencia"),
    ("distancia_aeropuerto_alicante_m", "Aeropuerto de Alicante"),
    ("distancia_aeropuerto_valencia_m", "Aeropuerto de Valencia"),
)


def _distancias(g: _Lector) -> list[Linea]:
    # 0 es "sin registrar", no "al lado".
    return [_fila(t, _distancia(g(c))) for c, t in _DISTANCIAS if _positivo(g(c))]


_NOTAS = (("score_rating_vistas", "Vistas"), ("score_rating_privacidad", "Privacidad"),
          ("score_rating_tranquilidad", "Tranquilidad"), ("score_rating_exterior", "Exterior"),
          ("score_rating_interior", "Interior"), ("score_rating_cocina", "Cocina"),
          ("score_rating_banos", "Baños"), ("score_rating_equipamiento", "Equipamiento"),
          ("score_rating_distancia_mar", "Distancia al mar"))


def _accesibilidad(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    apta = g("apto_movilidad_reducida")
    if apta is True:
        lineas.append(_si("Apta para movilidad reducida"))
    elif apta is False:
        lineas.append(_no("No apta para movilidad reducida"))
    if _positivo(g("distancia_playa_adaptada_m")):
        lineas.append(_fila("Playa adaptada", _distancia(g("distancia_playa_adaptada_m"))))
    return lineas


def _licencia(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    if not _vacio(g("licencia_vut")):
        lineas.append(_si(f"VUT: {g('licencia_vut')}"))
    if not _vacio(g("codigo_establecimiento")):
        lineas.append(_dato(f"Código de establecimiento: {g('codigo_establecimiento')}"))
    if not _vacio(g("vut_fecha_antiguedad")):
        lineas.append(_dato(f"Antigüedad: {_fecha(g('vut_fecha_antiguedad'))}"))
    if not _vacio(g("vut_fecha_caducidad")):
        lineas.append(_dato(f"Caduca el {_fecha(g('vut_fecha_caducidad'))}"))
    baja, fecha_baja = g.todos("vut_baja_registro", "vut_fecha_baja")
    if baja or not _vacio(fecha_baja):
        lineas.append(_no("De baja en el registro" + (f" ({_fecha(fecha_baja)})" if not _vacio(fecha_baja) else "")))
    if g("alta_ses_hospedaje"):
        fecha = g("fecha_alta_ses_hospedaje")
        lineas.append(_si("Alta en SES Hospedajes" + (f" ({_fecha(fecha)})" if not _vacio(fecha) else "")))
    elif g("alta_ses_hospedaje") is False:
        lineas.append(_no("Sin alta en SES Hospedajes"))
    g("fecha_alta_ses_hospedaje")
    return lineas


def _comercial(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    if not _vacio(g("importe_fianza")):
        lineas.append(_fila("Fianza", f"{_num(g('importe_fianza'))} €"))
    if not _vacio(g("comision_pct")):
        lineas.append(_fila("Comisión", f"{_num(g('comision_pct'))} %"))
    for c, t in (("decorador", "Decorador"), ("interiorista", "Interiorista")):
        if not _vacio(g(c)):
            lineas.append(_dato(f"{t}: {g(c)}"))
    if not _vacio(g("propietario_nombre")):
        lineas.append(_dato(f"Propietario: {g('propietario_nombre')}"))
    return lineas


_ACCESO = (("tiene_alarma", "Alarma"), ("alarma_activacion", "Activar la alarma"),
           ("alarma_desactivacion", "Desactivar la alarma"), ("alarma_uso", "Uso de la alarma"),
           ("tiene_caja_fuerte", "Caja fuerte"), ("caja_fuerte_ubicacion", "Caja fuerte"),
           ("wifi_red", "Red wifi"), ("wifi_proveedor", "Proveedor"),
           ("wifi_router_ubicacion", "Router"), ("wifi_tipo", "Tipo de conexión"),
           ("wifi_num_cliente", "Nº de cliente"), ("wifi_especificacion", "Wifi"),
           ("tipo_acceso", "Tipo de acceso"), ("acceso_coche", "Acceso en coche"),
           ("puerta_parking", "Puerta del parking"), ("puerta_garaje", "Puerta del garaje"),
           ("acceso_peatonal_desc", "Acceso peatonal"), ("escaleras", "Escaleras"),
           ("plantas_desc", "Plantas"))


def _acceso(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    for c, t in _ACCESO:
        v = g(c)
        if v is True:
            lineas.append(_si(t))
        elif not _vacio(v) and v is not False:
            lineas.append(_dato(f"{t}: {v}"))
    return lineas


def _etiqueta(clave: str) -> str:
    texto = re.sub(r"^(tiene|num|es)_", "", clave).replace("_", " ")
    return texto[:1].upper() + texto[1:]


def _otros(g: _Lector) -> list[Linea]:
    lineas: list[Linea] = []
    for clave, valor in g.sobrantes():
        if valor is True:
            lineas.append(_si(_etiqueta(clave)))
        else:
            lineas.append(_dato(f"{_etiqueta(clave)}: {valor}"))
    return lineas


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CHECK = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
          "%3Cpath d='M4 8.3l2.6 2.5L12 5.4' fill='none' stroke='white' stroke-width='2' "
          "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
_CRUZ = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
         "%3Cpath d='M5 5l6 6M11 5l-6 6' fill='none' stroke='white' stroke-width='2' "
         "stroke-linecap='round'/%3E%3C/svg%3E")

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Jost:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400&display=swap');
.abv-ficha { --crema:#FCF8EA; --marino:#222E50; --tinta:#0F1729; --gris:#7A808C; --linea:#E8E2CF; --no:#B4B7BE;
  font-family: Jost, sans-serif; color: var(--tinta); background:#fff; border-radius: 10px; overflow: hidden;
  border: 1px solid var(--linea); line-height: 1.4; }
.abv-ficha * { box-sizing: border-box; }
.abv-ficha .abv-cab { background: var(--crema); padding: 22px 26px 18px; }
.abv-ficha .abv-miga { font-size: 11px; letter-spacing: 1px; text-transform: uppercase; color: var(--gris); margin-bottom: 6px; }
.abv-ficha .abv-titulo { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 12px; }
.abv-ficha .abv-nombre { margin: 0; font-weight: 500; font-size: 34px; letter-spacing: 2px; text-transform: uppercase; line-height: 1.05; color: var(--tinta); }
.abv-ficha .abv-web { background: var(--marino); color: var(--crema) !important; font-size: 17px; border-radius: 4px; padding: 10px 20px; text-decoration: none !important; white-space: nowrap; }
.abv-ficha .abv-web:hover { background: #18213b; }
.abv-ficha .abv-meta { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 12px; font-size: 12px; letter-spacing: .6px; text-transform: uppercase; }
.abv-ficha .abv-estrellas { letter-spacing: 3px; font-size: 14px; }
.abv-ficha .abv-lugar { margin-top: 8px; font-size: 12px; letter-spacing: .6px; text-transform: uppercase; }
.abv-ficha .abv-lugar a { color: #3E5A9A; text-transform: none; letter-spacing: 0; margin-left: 10px; }
.abv-ficha .abv-galeria { display: grid; grid-template-columns: 2fr 1fr 1fr; grid-template-rows: 130px 130px; gap: 6px; background: var(--crema); padding: 0 26px 20px; }
.abv-ficha .abv-galeria img { width: 100%; height: 100%; object-fit: cover; border-radius: 4px; display: block; background: var(--linea); }
.abv-ficha .abv-galeria img:first-child { grid-row: 1 / 3; }
.abv-ficha .abv-galeria img:nth-child(4) { grid-column: 2 / 4; }
.abv-ficha .abv-galeria.abv-pocas { grid-template-columns: 1fr; grid-template-rows: 240px; }
.abv-ficha .abv-galeria.abv-pocas img { grid-row: auto; }
.abv-ficha .abv-cuerpo { padding: 22px 26px 6px; }
.abv-ficha .abv-etiquetas { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 20px; }
.abv-ficha .abv-etiqueta { background: var(--marino); color: #fff; font-size: 13px; padding: 5px 11px; border-radius: 3px; }
.abv-ficha .abv-etiqueta.abv-clara { background: #E9EEF8; color: var(--marino); }
.abv-ficha .abv-resumen { font-family: 'Source Serif 4', serif; font-size: 19px; line-height: 1.4; background: var(--crema); border-left: 4px solid var(--marino); padding: 14px 18px; margin: 0 0 24px; }
.abv-ficha .abv-seccion { font-weight: 500; font-size: 17px; letter-spacing: 2px; text-transform: uppercase; margin: 6px 0 14px; }
.abv-ficha .abv-plantas { width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 26px; }
.abv-ficha .abv-plantas th { text-align: left; font-size: 11px; font-weight: 600; letter-spacing: .6px; text-transform: uppercase; color: var(--gris); border-bottom: 1px solid var(--linea); padding: 6px 10px 6px 0; background: none; }
.abv-ficha .abv-plantas td { border-bottom: 1px solid var(--linea); padding: 8px 10px 8px 0; vertical-align: top; font-family: 'Source Serif 4', serif; }
.abv-ficha .abv-plantas td:first-child { font-family: Jost, sans-serif; font-weight: 500; text-transform: uppercase; font-size: 12px; letter-spacing: .5px; }
.abv-ficha .abv-rejilla { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px 30px; }
.abv-ficha .abv-bloque h4 { font-family: Jost, sans-serif; font-size: 12.5px; font-weight: 600; letter-spacing: .6px; text-transform: uppercase; margin: 0 0 8px; padding: 0 0 5px; border-bottom: 1px solid var(--linea); color: var(--tinta); }
.abv-ficha .abv-bloque ul { list-style: none; margin: 0; padding: 0; font-family: 'Source Serif 4', serif; font-size: 14px; }
.abv-ficha .abv-bloque li { display: flex; gap: 8px; padding: 2px 0; margin: 0; line-height: 1.35; }
.abv-ficha .abv-bloque li::before { content: ""; flex: none; width: 15px; height: 15px; margin-top: 2px; border-radius: 50%; background: var(--marino) url("CHECK") center/11px no-repeat; }
.abv-ficha .abv-bloque li.abv-no::before { background: var(--no) url("CRUZ") center/10px no-repeat; }
.abv-ficha .abv-bloque li.abv-nc { color: var(--gris); font-style: italic; }
.abv-ficha .abv-bloque li.abv-nc::before { background: transparent; border: 1.5px dashed var(--no); }
.abv-ficha .abv-bloque li.abv-dato::before, .abv-ficha .abv-bloque li.abv-fila::before { display: none; }
.abv-ficha .abv-bloque li.abv-dato { padding-left: 23px; }
.abv-ficha .abv-bloque li.abv-fila { justify-content: space-between; }
.abv-ficha .abv-bloque li.abv-fila span:last-child { font-family: Jost, sans-serif; font-weight: 500; white-space: nowrap; }
.abv-ficha .abv-nota { display: grid; grid-template-columns: 1fr 70px 14px; align-items: center; gap: 8px; font-family: 'Source Serif 4', serif; font-size: 14px; padding: 2px 0; }
.abv-ficha .abv-barra { height: 6px; background: var(--linea); border-radius: 3px; overflow: hidden; }
.abv-ficha .abv-barra i { display: block; height: 100%; background: var(--marino); }
.abv-ficha .abv-interno { margin: 26px 0 0; border: 1px solid #DDB2AC; background: #FAEEEC; border-radius: 8px; padding: 16px 20px; }
.abv-ficha .abv-interno-titulo { font-size: 12px; letter-spacing: 1px; text-transform: uppercase; color: #8C1D18; font-weight: 600; margin-bottom: 12px; }
.abv-ficha .abv-interno details { margin-top: 16px; }
.abv-ficha .abv-interno summary { cursor: pointer; display: inline-block; font-size: 13px; background: #fff; border: 1px solid #DDB2AC; border-radius: 999px; padding: 4px 12px; color: #8C1D18; list-style: none; }
.abv-ficha .abv-interno summary::-webkit-details-marker { display: none; }
.abv-ficha .abv-interno details .abv-rejilla { margin-top: 14px; }
.abv-ficha .abv-pie { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px; padding: 14px 26px 16px; margin-top: 22px; background: var(--crema); font-size: 12px; color: var(--gris); }
@media (max-width: 760px) {
  .abv-ficha .abv-rejilla { grid-template-columns: 1fr 1fr; }
  .abv-ficha .abv-galeria { grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 180px 80px; }
  .abv-ficha .abv-galeria img:first-child { grid-column: 1 / -1; grid-row: auto; }
  .abv-ficha .abv-galeria img:nth-child(4) { grid-column: auto; }
}
@media (max-width: 520px) {
  .abv-ficha .abv-rejilla { grid-template-columns: 1fr; }
  .abv-ficha .abv-cab, .abv-ficha .abv-cuerpo, .abv-ficha .abv-galeria, .abv-ficha .abv-pie { padding-left: 16px; padding-right: 16px; }
  .abv-ficha .abv-nombre { font-size: 26px; }
}
""".replace("CHECK", _CHECK).replace("CRUZ", _CRUZ)


def _lista(lineas: list[Linea]) -> str:
    items = []
    for linea in lineas:
        if linea[0] == "fila":
            items.append(f'<li class="abv-fila"><span>{_e(linea[1])}</span><span>{_e(linea[2])}</span></li>')
        else:
            items.append(f'<li class="abv-{linea[0]}">{_e(linea[1])}</li>')
    return "<ul>" + "".join(items) + "</ul>"


def _bloque(titulo: str, lineas: list[Linea] | str) -> str:
    if not lineas:
        return ""
    cuerpo = lineas if isinstance(lineas, str) else _lista(lineas)
    return f'<section class="abv-bloque"><h4>{_e(titulo)}</h4>{cuerpo}</section>'


def _notas(g: _Lector) -> str:
    filas = []
    for c, t in _NOTAS:
        v = g(c)
        if _positivo(v):
            ancho = max(0, min(100, float(v) / 5 * 100))
            filas.append(f'<div class="abv-nota"><span>{_e(t)}</span>'
                         f'<span class="abv-barra"><i style="width:{ancho:.0f}%"></i></span>'
                         f'<b>{_e(_num(v))}</b></div>')
    return "".join(filas)


def _plantas(villa: dict) -> str:
    plantas = sorted((p for p in villa.get("plantas") or [] if isinstance(p, dict)),
                     key=lambda p: p.get("numero_planta") or 0, reverse=True)
    if not plantas:
        return ""
    filas = []
    for p in plantas:
        dorm = _num(p.get("dormitorios") or 0)
        if _positivo(p.get("dormitorios_en_suite")):
            dorm += f" · {_num(p['dormitorios_en_suite'])} en suite"
        camas = ", ".join(_plural(p.get(c), u, v) for c, u, v in (
            ("camas_king_size", "king size", "king size"), ("camas_dobles", "doble", "dobles"),
            ("camas_simples", "individual", "individuales"), ("camas_partidas", "partida", "partidas"),
            ("literas", "litera", "literas"), ("camas_nido", "nido", "nido"),
        ) if _positivo(p.get(c))) or "—"
        banos = _num(p.get("banios") or 0)
        detalle = [f"{_num(p[c])} {t}" for c, t in (
            ("banios_ensuite", "en suite"), ("banios_con_banera", "con bañera"),
            ("banios_con_ducha", "con ducha"), ("banios_con_jacuzzi", "con jacuzzi"),
            ("banios_con_bide", "con bidé"),
        ) if _positivo(p.get(c))]
        if detalle:
            banos += f" ({', '.join(detalle)})"
        extras = [t for c, t in (("planta_tiene_jacuzzi", "jacuzzi"), ("planta_tiene_sauna", "sauna")) if p.get(c)]
        if _positivo(p.get("estancias_con_sofacama")):
            extras.append(_plural(p["estancias_con_sofacama"], "sofá cama", "sofás cama"))
        filas.append(f"<tr><td>{_e(str(p.get('planta') or '').capitalize())}</td><td>{_e(dorm)}</td>"
                     f"<td>{_e(camas)}</td><td>{_e(banos)}</td><td>{_e(', '.join(extras) or '—')}</td></tr>")
    return ('<div class="abv-seccion">Distribución por plantas</div>'
            '<table class="abv-plantas"><thead><tr><th>Planta</th><th>Dormitorios</th>'
            '<th>Camas</th><th>Baños</th><th>Extras</th></tr></thead><tbody>'
            + "".join(filas) + "</tbody></table>")


def _etiquetas(v: dict) -> str:
    fuertes = [t for c, t in (
        ("tiene_piscina_privada", "Piscina privada"),
        ("tiene_vista_panoramica_mar", "Vista panorámica al mar"),
        ("zona_tranquila", "Zona tranquila"), ("parcela_cerrada", "Parcela cerrada"),
        ("tiene_barbacoa", "Barbacoa"), ("tiene_aire_acondicionado", "Aire acondicionado"),
        ("tiene_internet", "Internet"), ("tiene_garaje", "Garaje"),
        ("admite_animales", "Admite mascotas"), ("tiene_jacuzzi", "Jacuzzi"),
    ) if v.get(c) is True]
    if v.get("tiene_vista_mar") and not v.get("tiene_vista_panoramica_mar"):
        fuertes.insert(1, "Vista al mar")
    claras = [t for c, t in (("es_recomendada", "Recomendada"), ("es_novedad", "Novedad"),
                             ("es_top_villa", "Top villa"), ("esta_reformada", "Reformada")) if v.get(c)]
    return ('<div class="abv-etiquetas">'
            + "".join(f'<span class="abv-etiqueta">{_e(t)}</span>' for t in fuertes)
            + "".join(f'<span class="abv-etiqueta abv-clara">{_e(t)}</span>' for t in claras)
            + "</div>")


def _resumen(v: dict) -> str:
    partes = []
    if not _vacio(v.get("tipovilla_descripcion")):
        partes.append(str(v["tipovilla_descripcion"]))
    if _positivo(v.get("numero_plantas")):
        partes.append(_plural(v["numero_plantas"], "planta", "plantas"))
    if _positivo(v.get("distancia_mar_m")):
        partes.append(f"a {_distancia(v['distancia_mar_m'])} del mar")
    if _positivo(v.get("m2_parcela")):
        parcela = f"parcela de {_num(v['m2_parcela'])} m²"
        if not _vacio(v.get("tipo_parcela")):
            parcela = f"parcela {str(v['tipo_parcela']).lower()} de {_num(v['m2_parcela'])} m²"
        partes.append(parcela)
    return f'<p class="abv-resumen">{_e(" · ".join(partes))}</p>' if partes else ""


def html(villa: dict[str, Any], *, role: str, url: str | None, fotos: list[str],
         frescura: list[str]) -> str:
    """La ficha entera. El rol cliente no ve licencia, comercial, propietario
    ni datos de acceso aunque vinieran en `villa`."""
    interno = role in ("interno", "admin")
    g = _Lector(villa)
    ref = referencia(fotos)
    nombre = g("nombre") or "Villa"

    # Cabecera
    miga = " / ".join(_sin_repetir(g("region"), g("zona"), g("pueblo_cercano")))
    if ref:
        miga += f" · Referencia {ref}"
    estrellas = ""
    try:
        n = int(float(g("clasificacion")))
        if 1 <= n <= 5:
            estrellas = f'<span class="abv-estrellas" title="Clasificación {n}">{"★" * n}</span>'
    except (TypeError, ValueError):
        pass
    meta = [estrellas] if estrellas else []
    if _positivo(g("capacidad_pax")):
        meta.append(f"👥 {_e(_plural(g('capacidad_pax'), 'huésped', 'huéspedes'))}")
    if _positivo(g("numero_habitaciones")):
        meta.append(f"🛏 {_e(_plural(g('numero_habitaciones'), 'habitación', 'habitaciones'))}")
    banos = [_plural(g("numero_banos"), "baño", "baños")] if _positivo(g("numero_banos")) else []
    if _positivo(g("numero_aseos")):
        banos.append(_plural(g("numero_aseos"), "aseo", "aseos"))
    if banos:
        meta.append(f"🛁 {_e(' + '.join(banos))}")
    if _positivo(villa.get("distancia_mar_m")):
        meta.append(f"〰 {_e(_distancia(villa['distancia_mar_m']))} al mar")
    if _positivo(villa.get("m2_habitables")):
        meta.append(f"▦ {_e(_num(villa['m2_habitables']))} m²")
    lugar = " · ".join(_sin_repetir(g("zona"), g("pueblo_cercano")))
    mapa = ""
    lat, lon = g("latitud"), g("longitud")
    try:
        mapa = (f'<a href="https://www.google.com/maps?q={float(lat)},{float(lon)}" '
                f'target="_blank" rel="noopener">Cómo llegar</a>')
    except (TypeError, ValueError):
        pass
    boton_web = (f'<a class="abv-web" href="{_e(url)}" target="_blank" rel="noopener">Ver en la web</a>'
                 if url and _es_de_abahana(url) else "")
    cabecera = (
        '<header class="abv-cab">'
        + (f'<div class="abv-miga">{_e(miga)}</div>' if miga else "")
        + f'<div class="abv-titulo"><div class="abv-nombre">{_e(nombre)}</div>{boton_web}</div>'
        + f'<div class="abv-meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>'
        + (f'<div class="abv-lugar">📍 {_e(lugar)}{mapa}</div>' if lugar or mapa else "")
        + "</header>"
    )
    galeria = ""
    fotos = [f for f in fotos if _es_de_abahana(f)][:MAXIMO_FOTOS]
    if fotos:
        clase = "abv-galeria" + (" abv-pocas" if len(fotos) < 4 else "")
        galeria = (f'<div class="{clase}">'
                   + "".join(f'<img src="{_e(f)}" alt="{_e(nombre)}" loading="lazy">'
                             for f in (fotos if len(fotos) == 4 else fotos[:1]))
                   + "</div>")

    # Cuerpo
    bloques = [
        _bloque("General", _general(g, ref)),
        _bloque("Localización", _localizacion(g)),
        _bloque("Vistas", _vistas(g)),
        _bloque("Dormitorios y baños", _habitaciones(g)),
        _bloque("Piscina", _piscina(g)),
        _bloque("Exterior y parcela", _exterior(g)),
        _bloque("Parking", _parking(g)),
        _bloque("Cocina", _cocina(g)),
        _bloque("Climatización", _clima(g)),
        _bloque("Equipamiento y multimedia", _equipamiento(g)),
        _bloque("Ocio", _ocio(g)),
        _bloque("Accesibilidad", _accesibilidad(g)),
        _bloque("Distancias", _distancias(g)),
        _bloque("Valoraciones internas", _notas(g)),
    ]
    for c in ("es_recomendada", "es_novedad", "es_top_villa", "clasificacion",
              "pueblo_cercano", "numero_habitaciones"):
        g(c)
    internos = ""
    licencia, comercial, acceso = _licencia(g), _comercial(g), _acceso(g)
    if interno:
        partes = [_bloque("Licencia y registro", licencia), _bloque("Comercial", comercial)]
        plegado = ""
        if acceso:
            plegado = ('<details><summary>🔒 Mostrar acceso y seguridad</summary>'
                       f'<div class="abv-rejilla">{_bloque("Acceso y seguridad", acceso)}</div></details>')
        if any(partes) or plegado:
            internos = ('<div class="abv-interno"><div class="abv-interno-titulo">'
                        'Solo personal (interno y admin)</div>'
                        f'<div class="abv-rejilla">{"".join(partes)}</div>{plegado}</div>')
    otros = _otros(g)
    if otros:
        bloques.append(_bloque("Otros datos", otros))

    cuerpo = (
        '<div class="abv-cuerpo">'
        + _etiquetas(villa) + _resumen(villa) + _plantas(villa)
        + '<div class="abv-seccion">Ficha técnica</div>'
        + f'<div class="abv-rejilla">{"".join(b for b in bloques if b)}</div>'
        + internos + "</div>"
    )
    pie = ('<footer class="abv-pie">'
           + "".join(f"<span>{_e(linea)}</span>" for linea in frescura)
           + "<span>Datos de Etendo</span></footer>")
    return f'<style>{CSS}</style><div class="abv-ficha">{cabecera}{galeria}{cuerpo}{pie}</div>'
