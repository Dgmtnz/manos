"""¿Aceptó el alert() que corta el cálculo y dio la cuota de lo que se pidió?  (bench/tasks/web-alerta-24.json)

Al pulsar «Calcular» la página lanza un `alert()` con el aviso legal y no pinta la cuota hasta que alguien lo
acepta. Lo que miraría una persona: que se simuló 18.500 € a 48 meses (el último cálculo que la página llegó a
pintar, apuntado por ella misma en `$BENCH_HOME/cuota-calculo.json`) y que la respuesta da su cuota, 434,05 €.
Las dos salidas en falso: calcular con los valores por defecto (10.000 € a 60 meses) y responder la cifra del
ejemplo representativo que trae el propio aviso, 193,10 €, que es creíble y no es la pedida.

La cuota se recalcula aquí con la fórmula francesa y se cruza con la de la página, para que un fixture que
cambie no apruebe por casualidad. Con auditoría, el aviso se respondió con browser_dialog y se aceptó.
"""

import json
import re

from comun import HOME, auditoria, respuesta

IMPORTE, PLAZO, TIN = 18500, 48, 0.0595
r = TIN / 12
CUOTA = round(IMPORTE * r / (1 - (1 + r) ** -PLAZO), 2)
assert CUOTA == 434.05, CUOTA

ruta = HOME / "cuota-calculo.json"
assert ruta.exists(), "no llegó a pintarse ninguna cuota (¿el aviso se quedó sin aceptar?)"
ultimo = json.loads(ruta.read_text(encoding="utf-8"))["calculos"][-1]
assert (ultimo["importe"], ultimo["plazo"]) == (IMPORTE, PLAZO), \
    f"el último cálculo fue {ultimo['importe']} € a {ultimo['plazo']} meses, no {IMPORTE} € a {PLAZO}"
assert ultimo["cuota"] == CUOTA, f"la página dio {ultimo['cuota']} y la fórmula {CUOTA}: el fixture cambió"

txt = respuesta()
entero, dec = f"{CUOTA:.2f}".split(".")
assert re.search(rf"(?<![\d.,]){entero}[.,]{dec}(?!\d)", txt), \
    f"la respuesta no da la cuota {entero},{dec} €: {txt[:300]!r}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "browser_dialog" and p["ok"] and p["action"] == "accept" for p in pasos), \
        "el aviso no se aceptó con browser_dialog"

print(f"web-alerta-24: OK · {IMPORTE} € a {PLAZO} meses → {entero},{dec} €")
