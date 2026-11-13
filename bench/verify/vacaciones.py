"""¿Pidió las vacaciones de Lucía bien, una sola vez, y dijo los días que cuenta el resumen?  (asistente-vacaciones-17)

Lo que miraría una persona en el programa: una única solicitud enviada a nombre de Lucía Gómez Prieto, de
Sistemas (no el Ventas que viene marcado), del 16/03/2026 al 20/03/2026. Las fechas de la instrucción vienen
como 2026-03-16 y el asistente solo admite DD/MM/AAAA: hay que leer su error y corregir. La respuesta tiene que
dar los 4 días laborables que muestra el resumen (el 19 de marzo es festivo), no los 5 de contar de lunes a
viernes. Lo apunta el propio asistente en `$XDG_DATA_HOME/bench/vacaciones.json` (bench/fixtures/vacaciones.py),
con su pid. Con auditoría, que se rellenó por la interfaz de esa ventana.
"""

import json
import os
import re
from pathlib import Path

from comun import auditoria, principal, respuesta

PEDIDO = {"nombre": "Lucía Gómez Prieto", "departamento": "Sistemas", "desde": "16/03/2026", "hasta": "20/03/2026",
          "dias": 4}

DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
st = json.loads((DIR / "vacaciones.json").read_text(encoding="utf-8"))
assert st["pid"] == int((DIR / "vacaciones.pid").read_text()), "vacaciones.json no lo escribió la ventana en pantalla"
envios = st["envios"]
assert envios, f"no se envió ninguna solicitud (el asistente se quedó en el paso {st['paso']})"
assert len(envios) == 1, f"se enviaron {len(envios)} solicitudes: duplicada"
for k, quiero in PEDIDO.items():
    assert envios[0][k] == quiero, f"{k}: se envió {envios[0][k]!r} en vez de {quiero!r}"

txt = respuesta()
assert re.search(r"(?<![\d.,])(4|cuatro)\s+dias|dias\s+laborables[^.\n\d]{0,40}?(4|cuatro)(?![.,]?\d)", txt), f"la respuesta no da los 4 días laborables: {txt[:300]!r}"
assert not re.search(r"(?<![\d.,])(5|cinco)\s+dias\s+laborables", principal(txt)), \
    f"la respuesta da 5 días laborables: no leyó el resumen: {txt[:300]!r}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "ui_act" and p["ok"] and p["resource_class"] == "manos-vacaciones" for p in pasos), \
        "ninguna ui_act en la ventana del asistente"

print("asistente-vacaciones-17: OK · errores vistos:", len(st["errores"]))
