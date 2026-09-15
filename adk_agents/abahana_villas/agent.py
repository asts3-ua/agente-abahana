"""Punto de entrada para `adk web`.

ADK toma el nombre del agente del nombre de la carpeta y exige que sea un
identificador Python válido. La carpeta del repo se llama `agente-abahana`,
con guion, así que ADK la rechazaba y todos los mensajes daban 404 aunque la
sesión se creara correctamente. Este paquete aporta un nombre válido.
"""

import sys
from pathlib import Path

# `adk web` solo añade al sys.path el directorio de agentes, no la raíz del
# repo, que es donde vive el agent.py de verdad.
_RAIZ = Path(__file__).resolve().parents[2]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from agent import root_agent  # noqa: E402

__all__ = ["root_agent"]
