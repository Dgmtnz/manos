"""¿Retomó la tarea y avisó solo de los pedidos que faltaban, una vez cada uno?  (pedidos-reanudar-20)

Lo que miraría una persona en el registro de avisos: cada pedido del 4711 al 4716 avisado exactamente una vez;
los de esta sesión (posteriores a `.t0`) son justo 4713, 4715 y 4716. El 4713 es la trampa: la sesión anterior
lo saltó a propósito (lo dice su nota) y quien siga «desde el último hecho» lo pierde. La ventana no enseña qué
se avisó: solo el checkpoint sembrado lo sabe. Además `avisos-sesion.txt`, escrito y guardado desde KWrite, con
esos tres números de menor a mayor, y la tarea de manos con los tres avisos marcados como hechos. Lo apunta el
programa en `$XDG_DATA_HOME/bench/pedidos.json` (bench/fixtures/pedidos.py) y la tarea vive en
`$XDG_DATA_HOME/manos/tasks/avisos-pedidos.json`.
"""

import json
import os
from collections import Counter
from pathlib import Path

from comun import HOME, auditoria

NUEVOS = [4713, 4715, 4716]
DATA = Path(os.environ["XDG_DATA_HOME"])

st = json.loads((DATA / "bench/pedidos.json").read_text(encoding="utf-8"))
assert st["pid"] == int((DATA / "bench/pedidos.pid").read_text()), "pedidos.json no es de la ventana en pantalla"
t0 = float((HOME / ".t0").read_text())
veces = Counter(a["pedido"] for a in st["avisos"])
dobles = sorted(p for p, n in veces.items() if n > 1)
assert not dobles, f"pedidos avisados más de una vez: {dobles}"
hoy = sorted(a["pedido"] for a in st["avisos"] if a["ts"] >= t0)
assert hoy == NUEVOS, f"en esta sesión se avisó {hoy} en vez de {NUEVOS}"

fichero = HOME / "avisos-sesion.txt"
assert fichero.is_file(), "no hay avisos-sesion.txt en el directorio de trabajo"
leido = fichero.read_text(encoding="utf-8")
assert leido.strip("\n") == "\n".join(map(str, NUEVOS)), f"avisos-sesion.txt dice {leido!r}"

tarea = json.loads((DATA / "manos/tasks/avisos-pedidos.json").read_text(encoding="utf-8"))
falta = [f"avisar del pedido {p}" for p in NUEVOS if f"avisar del pedido {p}" not in tarea["done"]]
assert not falta, f"la tarea no tiene marcados como hechos: {falta}"

pasos = auditoria()
if pasos is not None:
    en_kwrite = [p for p in pasos if p["ok"] and p["resource_class"] in ("org.kde.kwrite", "org.kde.kate")]
    assert any(p["tool"] == "ui_act" and p["action"] in ("set_text", "type") for p in en_kwrite), \
        "avisos-sesion.txt no se escribió desde KWrite"
    assert any((p["tool"] == "app_invoke" and (p["action"] or "").startswith("file_save"))
               or (p["tool"] == "ui_act" and p["action"] == "press") for p in en_kwrite), \
        "avisos-sesion.txt no se guardó desde KWrite"

print("pedidos-reanudar-20: OK")
