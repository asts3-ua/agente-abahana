"""Con qué datos y con qué fórmula ha salido cada número de la respuesta.

Un porcentaje de ocupación no se puede comprobar leyéndolo: "66 %" y "77 %"
son la misma villa contada con dos criterios distintos. Quien decide con ese
número necesita ver qué noches entraron en el numerador, cuáles en el
denominador y cuáles se quedaron fuera.

Todo lo que se enseña aquí sale del nombre de la herramienta, de los
argumentos con los que se la llamó y de los números que devolvió. Nunca del
texto del modelo: si el modelo resume una cosa y la herramienta calculó otra,
la diferencia se ve.

Hermano de `filtros.py` (qué buscó) y `frescura.py` (de cuándo son los datos).
"""

from __future__ import annotations

import datetime
from typing import Any, Iterable

# Mismo criterio que `agent._ESTADOS_FUERA_DE_VENTA`, repetido aquí para no
# importar el agente (y por tanto BigQuery) desde la capa de presentación. Un
# test comprueba que los dos conjuntos no se separen.
FUERA_DE_VENTA = {"No Disponible", "Reserva Propietario"}

_MESES = ("ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic")


def _num(valor: Any) -> str:
    """Un número como se escribe en español: 1.234 y 75,0."""
    if valor is None:
        return "—"
    if isinstance(valor, float) and not valor.is_integer():
        return f"{valor:,.2f}".replace(",", "~").replace(".", ",").replace("~", ".")
    return f"{int(valor):,}".replace(",", ".")


def _dia(valor: Any) -> str | None:
    if isinstance(valor, (datetime.date, datetime.datetime)):
        d = valor
    else:
        try:
            d = datetime.date.fromisoformat(str(valor)[:10])
        except (TypeError, ValueError):
            return None
    return f"{d.day} {_MESES[d.month - 1]} {d.year}"


def _periodo(datos: dict, args: dict) -> str:
    desde = _dia((datos.get("periodo") or {}).get("desde") or args.get("fecha_desde"))
    hasta = _dia((datos.get("periodo") or {}).get("hasta") or args.get("fecha_hasta"))
    if desde and hasta:
        return f"{desde} – {hasta}"
    return desde or hasta or ""


def _dict(valor: Any) -> dict:
    return valor if isinstance(valor, dict) else {}


# ---------------------------------------------------------------------------
# Ocupación
# ---------------------------------------------------------------------------

_FUENTE_OCUPACION = (
    "stg_etendo_Ocupacion — una fila por villa y noche, con su estado "
    "(Libre, Reserva, Reserva Agencia, Reserva Propietario, No Disponible)."
)


def _criterios_ocupacion(resumen: dict) -> list[str]:
    """Qué entra y qué no, con las noches de cada cosa: si alguien no está de
    acuerdo con el criterio, ve cuántas noches mueve la decisión."""
    vendidas = resumen.get("noches_vendidas")
    criterios = [
        f"**Cuentan como venta** las reservas directas, de agencia y de "
        f"turoperador: {_num(vendidas)} noches.",
        f"**Cuentan como disponibles** las noches libres: "
        f"{_num(resumen.get('noches_libres'))}.",
    ]
    bloqueadas = resumen.get("noches_bloqueadas") or 0
    propietario = resumen.get("noches_propietario") or 0
    fuera = []
    if propietario:
        fuera.append(f"{_num(propietario)} que el propietario usó su villa")
    if bloqueadas:
        fuera.append(f"{_num(bloqueadas)} bloqueadas (No Disponible)")
    if fuera:
        criterios.append(
            "**Quedan fuera del cálculo**, arriba y abajo, " + " y ".join(fuera)
            + ": esos días la villa no estaba a la venta, así que ni se "
              "vendieron ni se dejaron de vender."
        )
    return criterios


def _formula_ocupacion(resumen: dict) -> str | None:
    vendidas = resumen.get("noches_vendidas")
    comercializables = resumen.get("noches_comercializables")
    if not comercializables:
        return None
    pct = resumen.get("ocupacion_pct")
    return (
        f"`ocupación = noches vendidas ÷ noches comercializables`  \n"
        f"{_num(vendidas)} ÷ {_num(comercializables)} = **{_num(pct)} %**  \n"
        f"_(noches comercializables = {_num(vendidas)} vendidas + "
        f"{_num(resumen.get('noches_libres'))} libres)_"
    )


def _calendario(args: dict, datos: dict) -> dict | None:
    resumen = _dict(datos.get("resumen"))
    if not resumen:
        return None
    villa = datos.get("villa") or args.get("villa_nombre") or ""
    return {
        "titulo": " · ".join(x for x in ("Ocupación", villa, _periodo(datos, args)) if x),
        "fuentes": [_FUENTE_OCUPACION],
        "criterios": _criterios_ocupacion(resumen),
        "formula": _formula_ocupacion(resumen),
    }


def _resumen_ocupacion(args: dict, datos: dict) -> dict | None:
    totales = _dict(datos.get("totales"))
    if not totales:
        return None
    agrupacion = {"villa": "por villa", "zona": "por zona",
                  "mes": "por mes", "ano": "por año"}.get(datos.get("agrupar_por"), "")
    return {
        "titulo": " · ".join(x for x in ("Ocupación", agrupacion,
                                         _periodo(datos, args)) if x),
        "fuentes": [_FUENTE_OCUPACION],
        "criterios": _criterios_ocupacion(totales) + [
            "El total del conjunto se calcula sumando las noches de todas las "
            "villas, no promediando sus porcentajes: una villa con una noche "
            "no puede pesar igual que una con treinta."
        ],
        "formula": _formula_ocupacion(totales),
    }


# ---------------------------------------------------------------------------
# Reservas
# ---------------------------------------------------------------------------

_FUENTE_RESERVAS = (
    "stg_etendo_Reserva — una fila por reserva, con su estado, importe, "
    "fechas y titular."
)


def _criterios_reservas(args: dict) -> list[str]:
    criterios = []
    if args.get("solo_en_firme", True):
        criterios.append(
            "**Solo reservas en firme**: fuera las canceladas, anuladas, "
            "perdidas, prerreservas y borradores. El no show sí cuenta, "
            "porque se cobra."
        )
    else:
        criterios.append(
            "**Incluyendo** canceladas, anuladas, perdidas, prerreservas y "
            "borradores, porque se pidió expresamente."
        )
    if args.get("incluir_propietario"):
        criterios.append(
            "**Incluyendo** las estancias del propietario en su propia villa, "
            "que no facturan."
        )
    else:
        criterios.append(
            "**Sin las estancias del propietario** en su propia villa: no "
            "facturan y distorsionarían el importe medio."
        )
    return criterios


def _resumen_reservas(args: dict, datos: dict) -> dict | None:
    filas = datos.get("resumen")
    if filas is None:
        return None
    criterio = {"entrada": "la fecha de entrada", "confirmacion": "la fecha de confirmación",
                "creacion": "la fecha de creación"}.get(args.get("criterio_fecha", "entrada"))
    criterios = _criterios_reservas(args)
    if criterio:
        criterios.append(f"El periodo se filtra por **{criterio}**.")
    return {
        "titulo": " · ".join(x for x in ("Resumen de reservas",
                                         _periodo(datos, args)) if x),
        "fuentes": [_FUENTE_RESERVAS],
        "criterios": criterios,
        "formula": (
            "`importe medio = importe total ÷ nº de reservas`  \n"
            "`noches medias = noches totales ÷ nº de reservas`  \n"
            "`importe por noche = importe total ÷ noches totales`"
        ),
    }


def _consultar_reservas(args: dict, datos: dict) -> dict | None:
    total = datos.get("total")
    if total is None:
        return None
    return {
        "titulo": f"Reservas encontradas: {_num(total)}",
        "fuentes": [_FUENTE_RESERVAS],
        "criterios": _criterios_reservas(args),
        "formula": None,
    }


# ---------------------------------------------------------------------------
# Precio al cliente
# ---------------------------------------------------------------------------

def _precio_final(args: dict, datos: dict) -> dict | None:
    if datos.get("precio_final") is None:
        return None
    obligatorios = datos.get("obligatorios") or []
    lineas = [f"{_num(datos.get('alojamiento'))} € de alojamiento"]
    lineas += [f"{_num(o.get('importe'))} € de {o.get('concepto')}"
               for o in obligatorios if isinstance(o, dict)]
    return {
        "titulo": f"Precio final · {datos.get('villa') or ''}",
        "fuentes": [
            "Etendo en vivo (servicio `preciovilla`), no la copia nocturna: "
            "es la tarifa de este momento."
        ],
        "criterios": [
            "**Suma los extras obligatorios** (limpieza final y similares), "
            "que es lo que el cliente acaba pagando.",
            "**No suma los opcionales**: se listan aparte porque el cliente "
            "elige si los contrata.",
        ],
        "formula": (
            "`precio final = alojamiento + extras obligatorios`  \n"
            + " + ".join(lineas)
            + f" = **{_num(datos.get('precio_final'))} €**"
            + (f"  \n_{_num(datos.get('noches'))} noches, "
               f"{_num(datos.get('precio_medio_noche'))} € de media por noche_"
               if datos.get("noches") else "")
        ),
    }


# ---------------------------------------------------------------------------
# Disponibilidad (la única que también ve el cliente)
# ---------------------------------------------------------------------------

def _disponibilidad(args: dict, datos: dict) -> dict | None:
    total = datos.get("total_disponibles")
    if total is None:
        return None
    return {
        "titulo": " · ".join(x for x in (
            f"Villas libres: {_num(total)}", _periodo(datos, args)) if x),
        "fuentes": [
            "stg_etendo_Reserva — las reservas que ocupan la villa.",
            "stg_etendo_Ocupacion — el calendario día a día de cada villa.",
        ],
        "criterios": [
            "**Ocupan la villa** las reservas que siguen vivas. Una cancelada "
            "o un presupuesto que no se cerró no ocupan: la villa sale libre.",
            "**También cuenta el calendario**: una noche bloqueada (cierre, "
            "mantenimiento o uso del propietario) no está disponible aunque "
            "no haya ninguna reserva.",
            "Una noche sin estado conocido se da por **no libre**: decir que "
            "está libre una villa que no lo está es el peor error posible.",
            "Si la villa pide una **estancia mínima** mayor que las noches "
            "pedidas, no sale en el resultado.",
        ],
        "formula": None,
    }


def _ofertas(args: dict, datos: dict) -> dict | None:
    """Libres + precio: hereda los criterios de disponibilidad y añade de
    dónde sale el precio, que es lo que más se discute."""
    total = datos.get("total")
    if total is None:
        return None
    base = _disponibilidad(args, {"total_disponibles": total,
                                  "periodo": datos.get("periodo")}) or {}
    libres = datos.get("total_libres")
    criterios = list(base.get("criterios") or [])
    if libres is not None and libres != total:
        criterios.insert(0, (
            f"De **{_num(libres)} villas libres** en esas fechas, "
            f"**{_num(total)}** cumplen además el resto de lo pedido "
            f"(presupuesto, capacidad, equipamiento…)."
        ))
    criterios.append(
        "El **precio por noche** es la tarifa de la villa para cada noche del "
        "periodo. No incluye los extras obligatorios: para el total que paga "
        "el cliente está `precio_final_villa`, que los suma."
    )
    return {
        "titulo": " · ".join(x for x in (
            f"Villas libres con precio: {_num(total)}",
            _periodo(datos, args)) if x),
        "fuentes": (base.get("fuentes") or []) + [
            "stg_etendo_TarifaDia — la tarifa de cada villa noche a noche."
        ],
        "criterios": criterios,
        "formula": "`precio total = suma de la tarifa de cada noche del periodo`",
    }


# ---------------------------------------------------------------------------
# SQL a medida: la consulta ES el método
# ---------------------------------------------------------------------------

def _sql(args: dict, datos: dict) -> dict | None:
    query = (args or {}).get("query")
    if not query:
        return None
    return {
        "titulo": f"Consulta a medida · {_num(datos.get('count'))} filas",
        "fuentes": ["silver_clean, consultado directamente."],
        "criterios": [
            "Para esto no había herramienta, así que la consulta se escribió a "
            "medida. Los criterios son los que diga el SQL: conviene leerlo."
        ],
        "formula": None,
        "sql": query,
    }


_EXPLICAN = {
    "calendario_villa": _calendario,
    "resumen_ocupacion": _resumen_ocupacion,
    "resumen_reservas": _resumen_reservas,
    "consultar_reservas": _consultar_reservas,
    "precio_final_villa": _precio_final,
    "consultar_disponibilidad": _disponibilidad,
    "buscar_ofertas": _ofertas,
    "ejecutar_sql": _sql,
}


def explicar(llamadas: Iterable[tuple[str, dict, Any]]) -> list[dict]:
    """Un bloque por cada herramienta de la respuesta que haya hecho una
    cuenta. Las que solo buscan o devuelven texto no salen: no hay nada que
    comprobar en ellas, y llenar el desplegable de ruido es no enseñar nada.
    """
    bloques: list[dict] = []
    vistos: set[str] = set()
    for nombre, args, respuesta in llamadas or []:
        explica = _EXPLICAN.get(nombre)
        datos = _dict(respuesta)
        # Una llamada que falló no calculó nada: explicarla sería contar un
        # método que no se llegó a aplicar.
        if not explica or not datos or datos.get("error"):
            continue
        try:
            bloque = explica(args or {}, datos)
        except Exception:
            continue
        if not bloque:
            continue
        # Dos llamadas iguales (dos villas, mismo método) se explican una vez.
        firma = f"{nombre}|{bloque.get('titulo')}"
        if firma in vistos:
            continue
        vistos.add(firma)
        bloques.append(bloque)
    return bloques
