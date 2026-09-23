"""Enlaces directos a Etendo: abrir una villa o una reserva en su pantalla.

Etendo guarda la navegación en el fragmento de la URL (todo lo que va tras
`#`): una lista de vistas, cada una con su ventana, su pestaña y el id del
registro. Aquí se arma esa misma lista con los ids que ya tenemos en BigQuery
(`villa_id` y `reserva_id`).
"""

from __future__ import annotations

import os

# Ventana y pestaña de cada pantalla de Etendo (fijas; salen de la propia URL).
VENTANA_VILLAS = "E49F77D6324A407C8903330153C4AA2A"
PESTANA_VILLAS = "B0FB8208111A4A5E89F74EDD2CE660D2"
VENTANA_RESERVAS = "143"
PESTANA_RESERVAS = "186"

_ESPACIO_DE_TRABAJO = (
    "{viewId:__OBMyOpenbravoImplementation__,params:{myOB:true,canClose:false,"
    "tabTitle:__Espacio de Trabajo__}}"
)
_ESCAPES = {"{": "%7B", "}": "%7D", "[": "%5B", "]": "%5D", " ": "%20"}


def base_etendo() -> str:
    """URL de Etendo. La misma que usa la API (ETENDO_API_BASE_URL); se puede
    apuntar a otra con ETENDO_WEB_BASE_URL."""
    return (os.environ.get("ETENDO_WEB_BASE_URL")
            or os.environ.get("ETENDO_API_BASE_URL") or "").rstrip("/")


def _vista(ventana: str, pestana: str, titulo: str, registro: str) -> str:
    return (
        f"{{viewId:___{ventana}__,params:{{windowId:__{ventana}__,"
        f"viewId:___{ventana}__,tabTitle:__{titulo}__,"
        f"targetTabId:__{pestana}__,targetRecordId:__{registro}__}}}}"
    )


def _url(base: str | None, vistas: list[str]) -> str | None:
    base = (base if base is not None else base_etendo()).rstrip("/")
    if not base or not vistas:
        return None
    fragmento = (f"{{st:{len(vistas)},bm:["
                 + ",".join([_ESPACIO_DE_TRABAJO] + vistas) + "]}")
    for crudo, escapado in _ESCAPES.items():
        fragmento = fragmento.replace(crudo, escapado)
    return f"{base}/etendo/#{fragmento}"


def villa(villa_id: str | None, base: str | None = None) -> str | None:
    """Abre la villa en la pantalla de Villas de Etendo."""
    if not villa_id:
        return None
    return _url(base, [_vista(VENTANA_VILLAS, PESTANA_VILLAS, "Villas", villa_id)])


def reserva(reserva_id: str | None, villa_id: str | None = None,
            base: str | None = None) -> str | None:
    """Abre la reserva en Etendo. Si se sabe de qué villa es, se abre dentro de
    ella, como cuando se navega a mano."""
    if not reserva_id:
        return None
    vistas = []
    if villa_id:
        vistas.append(_vista(VENTANA_VILLAS, PESTANA_VILLAS, "Villas", villa_id))
    vistas.append(_vista(VENTANA_RESERVAS, PESTANA_RESERVAS, "Reservas", reserva_id))
    return _url(base, vistas)
