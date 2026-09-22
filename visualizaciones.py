"""Mapa y gráficos que acompañan a las respuestas del agente.

Todo sale de lo que devuelven las herramientas, nunca del texto del modelo: cada
punto del mapa y cada barra es un dato real. Las funciones de este módulo no
dependen de Streamlit salvo `render`, así que se pueden probar.

Paleta validada con el script de la guía de visualización sobre el blanco de las
tarjetas: azul de marca y naranja pasan banda de luminosidad, croma, separación
para daltonismo (también todas las parejas, que es lo que exige el calendario)
y contraste >= 3:1. El gris de bloqueos queda a 3,6:1; el fondo de los días
libres no es un dato con color propio y va siempre en leyenda y tabla.
"""
from __future__ import annotations

import datetime
from typing import Any, Callable, Iterable

import altair as alt
import pandas as pd
import pydeck as pdk

AZUL = "#3E5A9A"            # marca, serie principal
NARANJA = "#eb6834"         # segunda serie; separada del azul para daltonismo
GRIS_BLOQUEO = "#8A8780"
FONDO_LIBRE = "#E9E4D8"
TINTA = "#1E2B3C"
TINTA_SUAVE = "#4A5768"
REJILLA = "#E2E0D6"
SUPERFICIE = "#FFFFFF"
FUENTE = "Montserrat, system-ui, -apple-system, 'Segoe UI', sans-serif"

_HERRAMIENTAS_CON_VILLAS = {
    "buscar_propiedades", "listar_propiedades", "buscar_por_valoracion",
    "consultar_disponibilidad", "obtener_detalle_propiedad",
    "buscar_ofertas", "alternativas_villa",
}
_CAMPOS_VILLA = ("nombre", "pueblo_cercano", "capacidad_pax",
                 "numero_habitaciones", "tiene_piscina_privada")

_CATEGORIAS_CALENDARIO = ["Reserva de cliente", "Uso del propietario", "Bloqueada", "Libre"]
_COLORES_CALENDARIO = [AZUL, NARANJA, GRIS_BLOQUEO, FONDO_LIBRE]


# ---------------------------------------------------------------------------
# De respuestas de herramientas a visualizaciones
# ---------------------------------------------------------------------------

def recoger(llamadas: Iterable[tuple[str, dict, Any]]) -> list[dict]:
    """Visualizaciones para las herramientas usadas en un turno.

    Todas las villas encontradas en el turno van a un único mapa, sin repetir;
    precios, calendario y resumen dan un gráfico por llamada. Las respuestas
    con error o vacías no dan nada.
    """
    resultado: list[dict] = []
    villas: dict[str, dict] = {}
    for nombre, args, respuesta in llamadas:
        if not isinstance(respuesta, dict) or respuesta.get("error"):
            continue
        args = args or {}
        if nombre in _HERRAMIENTAS_CON_VILLAS:
            for match in respuesta.get("matches") or []:
                villa = match.get("nombre")
                if villa and villa not in villas:
                    villas[villa] = {k: match[k] for k in _CAMPOS_VILLA if match.get(k) is not None}
        elif nombre == "consultar_precios" and respuesta.get("noches"):
            resultado.append({
                "tipo": "precios",
                "villa": respuesta.get("villa") or args.get("villa_nombre"),
                "noches": respuesta["noches"],
                "resumen": respuesta.get("resumen") or {},
            })
        elif nombre == "calendario_villa" and respuesta.get("tramos"):
            resultado.append({
                "tipo": "calendario",
                "villa": respuesta.get("villa") or args.get("villa_nombre"),
                "tramos": respuesta["tramos"],
                "resumen": respuesta.get("resumen") or {},
            })
        elif nombre == "resumen_reservas" and respuesta.get("resumen"):
            resultado.append({
                "tipo": "resumen_reservas",
                "agrupar_por": str(args.get("agrupar_por") or "villa").lower(),
                "filas": respuesta["resumen"],
            })
    if villas:
        resultado.insert(0, {"tipo": "mapa", "villas": list(villas.values())})
    return resultado


def _coordenada(valor: Any, limite: float) -> float | None:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if -limite <= numero <= limite and numero != 0 else None


def anadir_coordenadas(
    mapa: dict, rol: str, consultar: Callable[[list[str]], list[dict]],
) -> dict | None:
    """Añade la posición de cada villa; None si ninguna tiene coordenadas.

    El cliente ve la posición redondeada a dos decimales (~1 km) y sin
    dirección: la herramienta que da los códigos de alarma al equipo interno no
    debe servir a la vez para localizar la casa exacta.
    """
    filas = {f.get("nombre"): f for f in consultar([v["nombre"] for v in mapa["villas"]])}
    aproximado = rol == "cliente"
    villas = []
    for villa in mapa["villas"]:
        fila = filas.get(villa["nombre"]) or {}
        lat = _coordenada(fila.get("latitud"), 90)
        lon = _coordenada(fila.get("longitud"), 180)
        if lat is None or lon is None:
            continue
        punto = dict(villa)
        punto.setdefault("pueblo_cercano", fila.get("pueblo_cercano"))
        if aproximado:
            lat, lon = round(lat, 2), round(lon, 2)
            punto.pop("direccion", None)
        elif fila.get("direccion"):
            punto["direccion"] = fila["direccion"]
        punto["lat"], punto["lon"] = lat, lon
        villas.append(punto)
    if not villas:
        return None
    return {"tipo": "mapa", "aproximado": aproximado, "villas": villas}


def consulta_coordenadas(bq, tabla_villa: str) -> Callable[[list[str]], list[dict]]:
    """Consulta de coordenadas por nombre de villa, lista para `anadir_coordenadas`."""
    from google.cloud import bigquery

    def consultar(nombres: list[str]) -> list[dict]:
        sql = f"""
            SELECT nombre, latitud, longitud, pueblo_cercano, direccion
            FROM {tabla_villa}
            WHERE nombre IN UNNEST(@nombres) AND es_activo = TRUE
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY nombre ORDER BY es_visible DESC, fecha_actualizacion DESC) = 1
        """
        trabajo = bq.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("nombres", "STRING", list(nombres))],
            maximum_bytes_billed=50 * 1024 * 1024,
        ))
        return [dict(fila.items()) for fila in trabajo.result()]

    return consultar


def categoria_calendario(tipo_ocupacion: str | None) -> str:
    """Tres clases de color y el fondo de libre.

    Con tramos que pueden quedar junto a cualquier otro, la guía limita a tres
    los colores: los canales de reserva van juntos y el canal se ve en el
    detalle y en la tabla.
    """
    tipo = (tipo_ocupacion or "").strip().lower()
    if tipo == "libre":
        return "Libre"
    if "propietario" in tipo:
        return "Uso del propietario"
    if tipo.startswith("reserva"):
        return "Reserva de cliente"
    # "No Disponible" y cualquier estado desconocido: no se puede vender.
    return "Bloqueada"


# ---------------------------------------------------------------------------
# Gráficos
# ---------------------------------------------------------------------------

def _estilo(chart: alt.Chart) -> alt.Chart:
    return (
        chart.configure(font=FUENTE, background=SUPERFICIE)
        .configure_axis(
            labelColor=TINTA_SUAVE, titleColor=TINTA_SUAVE, labelFontSize=11,
            titleFontSize=11, titleFontWeight=500, gridColor=REJILLA, gridWidth=1,
            domainColor=REJILLA, tickColor=REJILLA,
        )
        .configure_legend(
            orient="top", labelColor=TINTA, titleColor=TINTA_SUAVE, labelFontSize=12,
            titleFontSize=11, symbolType="circle", symbolSize=80,
        )
        .configure_view(strokeWidth=0)
    )


def grafico_precios(v: dict) -> alt.Chart:
    """Venta y compra por noche en un solo eje (€); el margen, en el detalle."""
    series = {"precio_venta": "Precio de venta", "precio_compra": "Precio de compra"}
    df = pd.DataFrame(v["noches"])
    df["fecha"] = pd.to_datetime(df["fecha"])
    for columna in ("precio_venta", "precio_compra", "margen", "margen_pct"):
        if columna not in df:
            df[columna] = None
    # Una fila por noche y serie. No con melt: venta y compra tienen que ir a
    # la vez como valor de la línea y en el detalle de cada punto, y melt
    # descarta las columnas que están en los dos lados (el gráfico salía vacío).
    largo = pd.concat(
        [df.assign(serie=etiqueta, valor=df[columna]) for columna, etiqueta in series.items()],
        ignore_index=True,
    ).dropna(subset=["valor"])
    largo["valor"] = largo["valor"].astype(float)

    color = alt.Color("serie:N", title=None, scale=alt.Scale(
        domain=list(series.values()), range=[AZUL, NARANJA]))
    x = alt.X("fecha:T", title=None,
              axis=alt.Axis(format="%d/%m", grid=False, labelOverlap=True, tickCount=6))
    y = alt.Y("valor:Q", title="€ por noche", axis=alt.Axis(format=",.0f"))
    cerca = alt.selection_point(fields=["fecha"], nearest=True, on="pointerover", empty=False)
    detalle = [
        alt.Tooltip("fecha:T", title="Noche", format="%d/%m/%Y"),
        alt.Tooltip("precio_venta:Q", title="Venta (€)", format=",.0f"),
        alt.Tooltip("precio_compra:Q", title="Compra (€)", format=",.0f"),
        alt.Tooltip("margen:Q", title="Margen (€)", format=",.0f"),
        alt.Tooltip("margen_pct:Q", title="Margen (%)", format=".1f"),
    ]

    base = alt.Chart(largo).encode(x=x, y=y, color=color)
    lineas = base.mark_line(strokeWidth=2, strokeCap="round", strokeJoin="round")
    zona = base.mark_point(opacity=0, size=400).encode(tooltip=detalle).add_params(cerca)
    puntos = base.mark_point(filled=True, size=64, stroke=SUPERFICIE, strokeWidth=2).encode(
        opacity=alt.condition(cerca, alt.value(1), alt.value(0)))
    guia = alt.Chart(largo).mark_rule(color=TINTA_SUAVE, opacity=0.35, strokeWidth=1).encode(
        x="fecha:T").transform_filter(cerca)
    return _estilo(alt.layer(lineas, zona, puntos, guia).properties(height=240))


_MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
          "Septiembre", "Octubre", "Noviembre", "Diciembre")
_CLASE_CALENDARIO = {
    "Reserva de cliente": "reserva", "Uso del propietario": "propietario",
    "Bloqueada": "bloqueada", "Libre": "libre",
}

_CSS_CALENDARIO = f"""
.abv-cal {{ font-family: {FUENTE}; color: {TINTA}; }}
.abv-cal .abv-cal-leyenda {{ display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; margin: 2px 0 10px; }}
.abv-cal .abv-cal-leyenda span {{ display: inline-flex; align-items: center; gap: 6px; }}
.abv-cal .abv-cal-leyenda i {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
.abv-cal .abv-cal-meses {{ display: flex; flex-wrap: wrap; gap: 14px 22px; }}
.abv-cal .abv-cal-mes {{ flex: 0 1 238px; }}
.abv-cal .abv-cal-titulo {{ font-size: 13px; font-weight: 600; margin: 0 0 6px 2px; }}
.abv-cal table {{ border-collapse: separate; border-spacing: 3px; width: 100%; table-layout: fixed; margin: 0; border: none; }}
.abv-cal th {{ font-size: 10.5px; font-weight: 600; color: {TINTA_SUAVE}; text-align: center; padding: 0 0 2px; border: none; background: none; }}
.abv-cal td {{ height: 30px; text-align: center; vertical-align: middle; font-size: 12px; font-weight: 500;
  border-radius: 5px; padding: 0; border: none; cursor: default; }}
.abv-cal td.abv-cal-vacio {{ background: transparent; }}
.abv-cal td.abv-cal-libre {{ background: {FONDO_LIBRE}; color: {TINTA}; }}
.abv-cal td.abv-cal-reserva {{ background: {AZUL}; color: #fff; }}
.abv-cal td.abv-cal-propietario {{ background: {NARANJA}; color: #fff; }}
.abv-cal td.abv-cal-bloqueada {{ background: {GRIS_BLOQUEO}; color: #fff; }}
.abv-cal td.abv-cal-fuera {{ background: transparent; color: #B9B6AE; font-weight: 400; }}
.abv-cal td.abv-cal-entrada {{ box-shadow: inset 3px 0 0 {SUPERFICIE}; }}
.abv-cal td.abv-cal-hoy {{ outline: 2px solid {TINTA}; outline-offset: -2px; }}
"""


def _dd_mm(fecha: datetime.date) -> str:
    return fecha.strftime("%d/%m")


def calendario_html(v: dict, hoy: datetime.date | None = None) -> str:
    """El calendario de una villa como un calendario de verdad: un mes por
    rejilla, de lunes a domingo, cada día con el color de su estado. Al pasar
    el ratón, el estado tal cual (el canal de la reserva) y las fechas del
    tramo. El primer día de cada reserva lleva una marca, para distinguir dos
    reservas seguidas."""
    import calendar
    from html import escape

    hoy = hoy or datetime.date.today()
    dias: dict[datetime.date, tuple[str, str, bool]] = {}
    previo = None
    for tramo in v.get("tramos") or []:
        try:
            desde = datetime.date.fromisoformat(str(tramo["desde"])[:10])
            hasta = datetime.date.fromisoformat(str(tramo["hasta"])[:10])
        except (KeyError, ValueError):
            continue
        estado = str(tramo.get("tipo_ocupacion") or "Sin dato")
        clase = _CLASE_CALENDARIO[categoria_calendario(estado)]
        noches = (hasta - desde).days + 1
        titulo = f"{estado} · {_dd_mm(desde)} – {_dd_mm(hasta)} ({noches} {'día' if noches == 1 else 'días'})"
        # Marca de entrada: una reserva que empieza justo tras otra del mismo color.
        entrada = previo == clase and clase != "libre"
        dia = desde
        while dia <= hasta:
            dias[dia] = (clase, titulo, entrada and dia == desde)
            dia += datetime.timedelta(days=1)
        previo = clase
    if not dias:
        return ""

    primero, ultimo = min(dias), max(dias)
    meses = []
    anio, mes = primero.year, primero.month
    while (anio, mes) <= (ultimo.year, ultimo.month):
        filas = []
        for semana in calendar.Calendar(firstweekday=0).monthdatescalendar(anio, mes):
            celdas = []
            for dia in semana:
                if dia.month != mes:
                    celdas.append('<td class="abv-cal-vacio"></td>')
                    continue
                if dia in dias:
                    clase, titulo, entrada = dias[dia]
                    clases = f"abv-cal-{clase}" + (" abv-cal-entrada" if entrada else "")
                else:
                    clases, titulo = "abv-cal-fuera", "Fuera del periodo consultado"
                if dia == hoy:
                    clases += " abv-cal-hoy"
                    titulo += " · hoy"
                celdas.append(f'<td class="{clases}" title="{escape(titulo)}">{dia.day}</td>')
            filas.append("<tr>" + "".join(celdas) + "</tr>")
        cabecera = "".join(f"<th>{d}</th>" for d in "LMXJVSD")
        meses.append(
            f'<div class="abv-cal-mes"><div class="abv-cal-titulo">{_MESES[mes - 1]} {anio}</div>'
            f"<table><thead><tr>{cabecera}</tr></thead><tbody>{''.join(filas)}</tbody></table></div>"
        )
        anio, mes = (anio + 1, 1) if mes == 12 else (anio, mes + 1)

    leyenda = "".join(
        f'<span><i style="background:{color}"></i>{etiqueta}</span>'
        for etiqueta, color in zip(_CATEGORIAS_CALENDARIO, _COLORES_CALENDARIO)
    )
    return (f"<style>{_CSS_CALENDARIO}</style><div class=\"abv-cal\">"
            f'<div class="abv-cal-leyenda">{leyenda}</div>'
            f'<div class="abv-cal-meses">{"".join(meses)}</div></div>')


def grafico_resumen(v: dict) -> alt.Chart:
    """Reservas por grupo, un solo color. Por tiempo: columnas en orden."""
    df = pd.DataFrame(v["filas"])
    df["dimension"] = df["dimension"].astype(str)
    detalle = [
        alt.Tooltip("dimension:N", title=v["agrupar_por"].capitalize()),
        alt.Tooltip("total_reservas:Q", title="Reservas", format=","),
    ]
    for columna, titulo in (("importe_total", "Importe total (€)"), ("importe_medio", "Importe medio (€)")):
        if columna in df:
            detalle.append(alt.Tooltip(f"{columna}:Q", title=titulo, format=",.0f"))

    if v["agrupar_por"] in ("mes", "ano"):
        df = df.sort_values("dimension")
        chart = alt.Chart(df).mark_bar(size=20, cornerRadiusEnd=4, color=AZUL).encode(
            x=alt.X("dimension:O", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("total_reservas:Q", title="Reservas"),
            tooltip=detalle,
        ).properties(height=240)
    else:
        chart = alt.Chart(df).mark_bar(size=18, cornerRadiusEnd=4, color=AZUL).encode(
            y=alt.Y("dimension:N", title=None, sort="-x"),
            x=alt.X("total_reservas:Q", title="Reservas"),
            tooltip=detalle,
        ).properties(height=max(120, 26 * len(df)))
    return _estilo(chart)


def _zoom(villas: list[dict]) -> float:
    lats = [v["lat"] for v in villas]
    lons = [v["lon"] for v in villas]
    extension = max(max(lats) - min(lats), max(lons) - min(lons))
    for tope, zoom in ((0.01, 13), (0.05, 11.5), (0.15, 10.5), (0.4, 9.5)):
        if extension < tope:
            return zoom
    return 8.5


# Id de la capa de villas: la selección del mapa llega agrupada por capa.
CAPA_VILLAS = "villas"


def mapa_deck(v: dict) -> pdk.Deck:
    """Puntos azules de marca con anillo blanco; el nombre al pasar el ratón."""
    villas = v["villas"]
    df = pd.DataFrame(villas)
    for columna in ("pueblo_cercano", "direccion", "capacidad_pax"):
        if columna not in df:
            df[columna] = ""
    df = df.fillna("")

    capa = pdk.Layer(
        "ScatterplotLayer",
        id=CAPA_VILLAS,
        data=df,
        get_position="[lon, lat]",
        get_fill_color=[62, 90, 154, 235],
        get_line_color=[255, 255, 255, 255],
        stroked=True,
        filled=True,
        line_width_min_pixels=2,
        radius_min_pixels=6,
        radius_max_pixels=14,
        get_radius=90,
        pickable=True,
        auto_highlight=True,
    )
    vista = pdk.ViewState(
        latitude=sum(x["lat"] for x in villas) / len(villas),
        longitude=sum(x["lon"] for x in villas) / len(villas),
        zoom=_zoom(villas),
    )
    return pdk.Deck(layers=[capa], initial_view_state=vista, tooltip=tooltip_mapa(v),
                    map_provider="carto", map_style="light")


def tooltip_mapa(v: dict) -> dict:
    """Detalle al pasar el ratón: nombre y pueblo; la dirección, solo al equipo."""
    html = "<b>{nombre}</b><br/>{pueblo_cercano}"
    if not v.get("aproximado") and any(villa.get("direccion") for villa in v["villas"]):
        html += "<br/>{direccion}"
    html += f'<br/><span style="color:{GRIS_BLOQUEO}">Pulsa para ver más información</span>'
    return {"html": html, "style": {
        "backgroundColor": SUPERFICIE, "color": TINTA, "fontFamily": FUENTE,
        "fontSize": "12px", "border": f"1px solid {REJILLA}", "borderRadius": "6px",
        "padding": "6px 8px",
        # Las direcciones largas se salían del borde del mapa.
        "maxWidth": "260px",
        "whiteSpace": "normal",
    }}


# ---------------------------------------------------------------------------
# Pintado en Streamlit
# ---------------------------------------------------------------------------

def _euros(valor: Any) -> str:
    if valor is None:
        return "—"
    return f"{valor:,.0f} €".replace(",", ".")


def _numero(valor: Any) -> str:
    return "—" if valor is None else f"{valor:,}".replace(",", ".")


def cifras_html(pares: Iterable[tuple[str, str]]) -> str:
    """Cifras resumen en una fila que salta de línea si no cabe.

    Con st.metric en columnas, la estrechez de la burbuja del chat las apilaba
    una bajo otra y ocupaban media pantalla.
    """
    from html import escape

    celdas = "".join(
        f'<div class="abv-cifra"><span class="abv-cifra-etiqueta">{escape(etiqueta)}</span>'
        f'<span class="abv-cifra-valor">{escape(valor)}</span></div>'
        for etiqueta, valor in pares
    )
    return f'<div class="abv-cifras">{celdas}</div>'


def villa_pulsada(evento: Any) -> str | None:
    """Nombre de la villa pulsada en el mapa, si hay alguna."""
    try:
        objetos = evento.selection.objects.get(CAPA_VILLAS) or []
    except AttributeError:
        return None
    nombre = objetos[0].get("nombre") if objetos and isinstance(objetos[0], dict) else None
    return nombre or None


def render(v: dict, key: str | None = None) -> str | None:
    """Pinta la visualización. En un mapa devuelve la villa pulsada, para
    abrir su desplegable con el botón de la ficha."""
    import streamlit as st

    tipo = v.get("tipo")
    if tipo == "mapa":
        villas = v["villas"]
        cuantas = f"{len(villas)} {'villa' if len(villas) == 1 else 'villas'} en el mapa"
        st.caption(
            ("Ubicación aproximada de las villas" if v.get("aproximado") else cuantas)
            + " · pulsa una para ver su información"
        )
        evento = st.pydeck_chart(mapa_deck(v), use_container_width=True, height=320,
                                 on_select="rerun", selection_mode="single-object", key=key)
        return villa_pulsada(evento)
    elif tipo == "precios":
        resumen = v.get("resumen") or {}
        st.caption(f"Precio por noche · {v.get('villa') or ''}")
        margen = resumen.get("margen_medio_pct")
        st.markdown(cifras_html([
            ("Venta del periodo", _euros(resumen.get("total_venta"))),
            ("Margen del periodo", _euros(resumen.get("margen_total"))),
            ("Margen medio", "—" if margen is None else f"{margen:.1f} %".replace(".", ",")),
        ]), unsafe_allow_html=True)
        st.altair_chart(grafico_precios(v), use_container_width=True, theme=None)
    elif tipo == "calendario":
        resumen = v.get("resumen") or {}
        st.caption(f"Calendario · {v.get('villa') or ''}")
        ocupacion = resumen.get("ocupacion_pct")
        st.markdown(cifras_html([
            ("Ocupación", "—" if ocupacion is None else f"{ocupacion:.0f} %"),
            ("Noches ocupadas", _numero(resumen.get("noches_ocupadas"))),
            ("Libres", _numero(resumen.get("noches_libres"))),
            ("Bloqueadas", _numero(resumen.get("noches_bloqueadas"))),
        ]), unsafe_allow_html=True)
        st.html(calendario_html(v))
    elif tipo == "resumen_reservas":
        st.caption(f"Reservas por {v['agrupar_por']}")
        st.altair_chart(grafico_resumen(v), use_container_width=True, theme=None)
    return None
