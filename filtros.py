"""Qué ha buscado el agente, contado desde los argumentos de cada herramienta.

Sirve para comprobar de un vistazo si el agente entendió la consulta
("Moraira · 6+ personas · del 3 al 10 oct 2026 · admite mascotas") antes de
decidir con esos datos. Sale de lo que de verdad se pasó a BigQuery, no del
texto del modelo: si el modelo resume una cosa y filtra otra, aquí se ve.
"""

from __future__ import annotations

import datetime
from typing import Any, Iterable

# Herramientas que buscan datos, con el nombre que ve el usuario. Las demás
# (fecha actual, web, internet, feedback) no filtran nada que comprobar.
_HERRAMIENTAS = {
    "listar_propiedades": "Villas",
    "buscar_propiedades": "Villas",
    "buscar_por_valoracion": "Valoraciones",
    "obtener_detalle_propiedad": "Ficha",
    "consultar_disponibilidad": "Disponibilidad",
    "buscar_ofertas": "Ofertas",
    "alternativas_villa": "Alternativas",
    "consultar_reservas": "Reservas",
    "resumen_reservas": "Resumen de reservas",
    "consultar_precios": "Precios",
    "calendario_villa": "Calendario",
    "ejecutar_sql": "Consulta SQL a medida",
}

_SI_NO = {
    "piscina": ("piscina privada", "sin piscina privada"),
    "admite_animales": ("admite mascotas", "no admite mascotas"),
    "internet": ("wifi", "sin wifi"),
    "aire_acondicionado": ("aire acondicionado", "sin aire acondicionado"),
    "lavadora": ("lavadora", "sin lavadora"),
    "lavavajillas": ("lavavajillas", "sin lavavajillas"),
    "vista_mar": ("vistas al mar", "sin vistas al mar"),
    "zona_tranquila": ("zona tranquila", "zona no tranquila"),
    "gimnasio": ("gimnasio", "sin gimnasio"),
    "accesible": ("accesible", "no accesible"),
    "jacuzzi": ("jacuzzi", "sin jacuzzi"),
    "billar": ("billar", "sin billar"),
    "futbolin": ("futbolín", "sin futbolín"),
    "parking": ("parking", "sin parking"),
    "garaje": ("garaje", "sin garaje"),
    "terraza": ("terraza", "sin terraza"),
}

_NOMBRES_EQUIPAMIENTO = {
    "pingpong": "ping pong", "padel": "pádel", "tobogan": "tobogán",
    "cama_elastica": "cama elástica", "jardin": "jardín", "cesped": "césped",
    "solarium": "solárium", "ducha_exterior": "ducha exterior",
    "futbolin": "futbolín",
}

_MINIMOS = {
    "capacidad_min": "{} personas",
    "habitaciones_min": "{} habitaciones",
    "camas_min": "{} camas",
    "banos_min": "{} baños",
    "metros_habitables_min": "{} m²",
}

_ESTADOS = {
    "RE": "reservas", "PE": "perdidas", "CA": "canceladas", "NS": "no shows",
    "PR": "prerreservas", "BO": "bloqueadas", "CO": "confirmadas",
    "DR": "borradores", "CL": "cerradas", "VO": "anuladas",
}

_MESES = ("ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic")

# Argumentos que no cambian qué se busca, solo cuánto se enseña.
_IGNORADOS = {"limite", "query"}


def _condicion_legible(condicion: Any) -> str:
    """"tiene_pingpong" -> "ping pong", "num_mosquiteras >= 2" -> "num mosquiteras ≥ 2"."""
    texto = str(condicion).strip()
    for op, bonito in ((">=", " ≥ "), ("<=", " ≤ "), ("!=", " ≠ ")):
        texto = texto.replace(op, bonito)
    texto = " ".join(texto.replace("|", " o ").split())
    palabras = []
    for palabra in texto.split(" "):
        clave = palabra.lower().removeprefix("tiene_")
        palabras.append(_NOMBRES_EQUIPAMIENTO.get(clave, clave.replace("_", " ")))
    return " ".join(palabras)


def _fecha(valor: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


# Con el día de la semana se ve si "este sábado" o "este fin de semana" se
# han entendido como el usuario quería.
_DIAS_SEMANA = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def _dia(d: datetime.date, con_anio: bool = True) -> str:
    texto = f"{_DIAS_SEMANA[d.weekday()]} {d.day} {_MESES[d.month - 1]}"
    return f"{texto} {d.year}" if con_anio else texto


def _rango(desde: Any, hasta: Any) -> str:
    """"del sáb 3 al sáb 10 oct 2026", "desde el sáb 3 oct 2026"..."""
    d1, d2 = _fecha(desde), _fecha(hasta)
    if d1 and d2:
        if d1 == d2:
            return f"el {_dia(d1)}"
        if (d1.year, d1.month) == (d2.year, d2.month):
            return f"del {_DIAS_SEMANA[d1.weekday()]} {d1.day} al {_dia(d2)}"
        if d1.year == d2.year:
            return f"del {_dia(d1, False)} al {_dia(d2)}"
        return f"del {_dia(d1)} al {_dia(d2)}"
    if d1:
        return f"desde el {_dia(d1)}"
    if d2:
        return f"hasta el {_dia(d2)}"
    return ""


def _fechas(nombre: str, args: dict) -> list[str]:
    partes = []
    desde, hasta = args.get("fecha_desde"), args.get("fecha_hasta")
    if desde or hasta:
        if nombre in ("consultar_disponibilidad", "buscar_ofertas") and desde and not hasta:
            partes.append(f"noche del {_dia(_fecha(desde))}" if _fecha(desde) else str(desde))
        else:
            prefijo = {"consultar_reservas": "entrada ",
                       "resumen_reservas": _prefijo_criterio(args)}.get(nombre, "")
            partes.append(prefijo + _rango(desde, hasta))
    if args.get("salida_desde") or args.get("salida_hasta"):
        partes.append("salida " + _rango(args.get("salida_desde"), args.get("salida_hasta")))
    if args.get("activa_en") and _fecha(args["activa_en"]):
        partes.append(f"ocupadas el {_dia(_fecha(args['activa_en']))}")
    if args.get("anulada_desde") or args.get("anulada_hasta"):
        partes.append("anuladas " + _rango(args.get("anulada_desde"), args.get("anulada_hasta")))
    if args.get("confirmada_desde") or args.get("confirmada_hasta"):
        partes.append("confirmadas " + _rango(args.get("confirmada_desde"), args.get("confirmada_hasta")))
    if args.get("creada_desde") or args.get("creada_hasta"):
        partes.append("hechas " + _rango(args.get("creada_desde"), args.get("creada_hasta")))
    criterio = str(args.get("criterio_fecha") or "").lower()
    if (nombre == "resumen_reservas" and criterio.startswith(("confirm", "crea"))
            and not (desde or hasta)):
        partes.append("por fecha de " + ("confirmación" if criterio.startswith("confirm") else "creación"))
    return partes


def _prefijo_criterio(args: dict) -> str:
    """En los resúmenes, qué fecha filtra: la de entrada salvo que se pida
    la de confirmación o la de creación."""
    criterio = str(args.get("criterio_fecha") or "").lower()
    if criterio.startswith("confirm"):
        return "confirmadas "
    if criterio.startswith("crea"):
        return "hechas "
    return "entrada "


def describir_llamada(nombre: str, args: dict | None) -> str | None:
    """Una línea con lo que filtró una llamada, o None si no es una búsqueda."""
    if nombre not in _HERRAMIENTAS:
        return None
    args = {k: v for k, v in (args or {}).items()
            if v not in (None, "", []) and k not in _IGNORADOS}
    partes: list[str] = []

    if args.get("localizador"):
        partes.append(f"nº {args['localizador']}")
    if args.get("titular"):
        partes.append(f"titular {args['titular']}")
    for clave in ("villa_nombre", "nombre"):
        if args.get(clave):
            partes.append(f"villa {str(args[clave]).upper()}")
    for clave in ("ubicacion", "zona", "direccion"):
        if args.get(clave):
            partes.append(str(args[clave]))
    partes += _fechas(nombre, args)
    for clave, plantilla in _MINIMOS.items():
        if args.get(clave) is not None:
            partes.append(plantilla.format(f"{args[clave]}+"))
    if args.get("presupuesto_max") is not None:
        try:
            importe = f"{float(args['presupuesto_max']):,.0f}".replace(",", ".")
        except (TypeError, ValueError):
            importe = str(args["presupuesto_max"])
        partes.append(f"hasta {importe} €")
    if args.get("orden") and args["orden"] != "precio":
        partes.append(f"orden: {args['orden']}")
    if str(args.get("ordenar_por") or "").lower().startswith(("importe", "precio")):
        partes.append("más caras primero")
    if args.get("rating_min") is not None:
        partes.append(f"valoración ≥ {args['rating_min']}")
    if args.get("distancia_mar_max_m") is not None:
        partes.append(f"a ≤ {args['distancia_mar_max_m']} m del mar")
    for clave, (si, no) in _SI_NO.items():
        if clave in args and isinstance(args[clave], bool):
            partes.append(si if args[clave] else no)
    for clave in ("estado_reserva", "estado_documento"):
        if args.get(clave):
            codigo = str(args[clave]).strip().upper()
            partes.append(_ESTADOS.get(codigo, codigo.lower()))
    if args.get("excluir_canceladas") is False:
        partes.append("incluye canceladas")
    if args.get("solo_en_firme") is False:
        partes.append("incluye no confirmadas")
    if args.get("incluir_propietario") is True:
        partes.append("incluye estancias del propietario")
    if args.get("agrupar_por"):
        partes.append(f"por {args['agrupar_por']}")
    if args.get("caracteristicas"):
        lista = args["caracteristicas"]
        if not isinstance(lista, (list, tuple)):
            lista = [lista]
        partes += [_condicion_legible(c) for c in lista]
    if args.get("secciones"):
        secciones = args["secciones"]
        if isinstance(secciones, (list, tuple)):
            secciones = ", ".join(map(str, secciones))
        partes.append(f"secciones: {secciones}")
    if args.get("texto"):
        partes.append(f"«{args['texto']}»")

    # Lo que no sabemos nombrar se enseña tal cual: mejor feo que oculto.
    conocidos = ({"villa_nombre", "nombre", "ubicacion", "zona", "direccion",
                  "fecha_desde", "fecha_hasta", "salida_desde", "salida_hasta",
                  "activa_en", "anulada_desde", "anulada_hasta", "rating_min",
                  "distancia_mar_max_m", "estado_reserva", "estado_documento",
                  "excluir_canceladas", "solo_en_firme", "incluir_propietario",
                  "agrupar_por", "secciones", "texto", "caracteristicas",
                  "presupuesto_max", "orden", "confirmada_desde", "confirmada_hasta",
                  "creada_desde", "creada_hasta", "criterio_fecha", "localizador",
                  "titular", "ordenar_por"}
                 | set(_MINIMOS) | set(_SI_NO))
    for clave, valor in args.items():
        if clave not in conocidos:
            partes.append(f"{clave}: {valor}")

    titulo = _HERRAMIENTAS[nombre]
    if not partes:
        return f"{titulo}: sin filtros" if nombre != "ejecutar_sql" else titulo
    return f"{titulo}: " + " · ".join(partes)


def describir(llamadas: Iterable[tuple[str, dict, Any]]) -> list[str]:
    """Una línea por búsqueda del turno, sin repetir, en el orden en que se hizo."""
    lineas: list[str] = []
    for nombre, args, _respuesta in llamadas:
        linea = describir_llamada(nombre, args)
        if linea and linea not in lineas:
            lineas.append(linea)
    return lineas
