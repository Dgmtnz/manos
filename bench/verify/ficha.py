"""¿Pasó los datos del CRM antiguo a la ficha nueva, en la ventana correcta, y la guardó?  (crm-dos-ventanas-19)

Lo que miraría una persona en el CRM nuevo: la ficha 1042 guardada con la razón social completa, el NIF, el
teléfono MÓVIL (no el fijo que traía) como +34 y nueve cifras (el formato que exige al guardar), el correo y la
dirección de ENVÍO (no la de facturación). Y el CRM antiguo intacto. Las dos ventanas son la misma app
(`manos-ficha`): solo el uuid de cada una las separa. Lo apunta cada instancia en
`$XDG_DATA_HOME/bench/ficha-<rol>.json` (bench/fixtures/ficha.py), con su pid. Vale el último guardado: guardar
dos veces la misma ficha no hace daño.
"""

import json
import os
from pathlib import Path

from comun import auditoria

QUIERO = {"Razón social": "Talleres Ebro Hermanos S.L.", "NIF": "B50123456", "Teléfono": "+34612345678",
          "Correo": "compras@talleresebro.example",
          "Dirección de envío": "Polígono Malpica, calle E, nave 7, 50016 Zaragoza"}
ANTIGUO = {"Razón social": "Talleres Ebro Hermanos S.L.", "NIF": "B50123456", "Teléfono fijo": "976 123 456",
           "Teléfono móvil": "612 345 678", "Correo": "compras@talleresebro.example",
           "Dirección de facturación": "Calle Mayor 12, 50001 Zaragoza",
           "Dirección de envío": "Polígono Malpica, calle E, nave 7, 50016 Zaragoza"}

DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
st = {}
for rol in ("antiguo", "nuevo"):
    st[rol] = json.loads((DIR / f"ficha-{rol}.json").read_text(encoding="utf-8"))
    assert st[rol]["pid"] == int((DIR / f"ficha-{rol}.pid").read_text()), f"ficha-{rol}.json no es de la ventana en pantalla"

assert st["antiguo"]["campos"] == ANTIGUO, f"se tocó la ficha del CRM antiguo: {st['antiguo']['campos']}"
guardados = st["nuevo"]["guardados"]
assert guardados, f"la ficha nueva no se guardó (rechazos: {st['nuevo']['rechazos']})"
for k, quiero in QUIERO.items():
    assert guardados[-1][k] == quiero, f"{k}: se guardó {guardados[-1][k]!r} en vez de {quiero!r}"

pasos = auditoria()
if pasos is not None:
    escritos = sum(p["tool"] == "ui_act" and p["ok"] and p["action"] in ("set_text", "type")
                   and p["resource_class"] == "manos-ficha" for p in pasos)
    assert escritos >= 4, f"solo {escritos} campos escritos por la interfaz en la ficha"

print("crm-dos-ventanas-19: OK · rechazos al guardar:", len(st["nuevo"]["rechazos"]))
