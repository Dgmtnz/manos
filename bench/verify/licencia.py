"""¿Activó la licencia una vez, con el código entero, y dijo el equipo que mostró?  (licencia-modal-21)

Lo que miraría una persona en el programa: una sola activación, a nombre de Ferretería Albar S.L. y con el
código QK7M-4RTX-9PLW completo. El campo no admite pegar y, a la tercera tecla, un aviso modal se queda el
foco: lo tecleado a partir de ahí se pierde aunque `sent` lo cuente, así que hay que cerrar el aviso, mirar lo
que llegó (la longitud del campo) y completarlo o reescribirlo. Activar con el código a medias es un fallo
aunque luego se repita bien. El aviso tiene que quedar cerrado con su botón, y la respuesta tiene que dar el
número de equipo que el programa enseña 3 s después de activar. Lo apunta el programa en
`$XDG_DATA_HOME/bench/licencia.json` (bench/fixtures/licencia.py). Con auditoría, que se abrió con app_launch y
que el código entró tecleado en esa ventana.
"""

import json
import os
import re
import zlib
from pathlib import Path

from comun import auditoria, respuesta

TITULAR, CODIGO = "Ferretería Albar S.L.", "QK7M-4RTX-9PLW"
EQUIPO = 10000 + zlib.crc32(CODIGO.encode()) % 90000

DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
assert (DIR / "licencia.json").exists(), "la app de licencias no llegó a abrirse"
st = json.loads((DIR / "licencia.json").read_text(encoding="utf-8"))
assert st["pid"] == int((DIR / "licencia.pid").read_text()), "licencia.json no es de la ventana en pantalla"
act = st["activaciones"]
assert act, f"no se activó la licencia (rechazos con el aviso abierto: {st['rechazados']})"
assert len(act) == 1, f"se activó {len(act)} veces: {[a['codigo'] for a in act]}"
assert act[0]["titular"] == TITULAR, f"titular: {act[0]['titular']!r} en vez de {TITULAR!r}"
assert act[0]["codigo"] == CODIGO, f"código: {act[0]['codigo']!r} en vez de {CODIGO!r}"
assert st["aviso"] == "cerrado", f"el aviso quedó {st['aviso']!r}"

txt = respuesta()
assert re.search(rf"(?<!\d){EQUIPO}(?!\d)", txt), f"la respuesta no da el equipo {EQUIPO}: {txt[:300]!r}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "app_launch" and p["ok"] and p["action"] == "manos-licencia" for p in pasos), \
        "la app no se abrió con app_launch"
    assert any(p["tool"] == "ui_act" and p["ok"] and p["action"] == "type" and p["resource_class"] == "manos-licencia"
               for p in pasos), "el código no entró tecleado en la ventana de licencias"

print(f"licencia-modal-21: OK · equipo {EQUIPO} · pegados bloqueados: {st['pegados']}")
