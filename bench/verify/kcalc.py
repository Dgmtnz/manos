"""¿Dio KCalc el resultado de (17 × 23) − 45, pulsando sus botones?  (bench/tasks/kcalc-cuenta-09.json)

Lo que comprobaría una persona: que la respuesta dice 346. La cuenta se puede hacer de cabeza, así que lo
que mide la tarea está en la auditoría: al menos ocho `press` aceptados por AT-SPI (peldaño 2) en la
ventana de KCalc (1 7 × 2 3 − 4 5 = son nueve). Teclear la cuenta por EIS es peldaño 3 y aquí no cuenta.
"""

import re

from comun import auditoria, respuesta

txt = respuesta()
assert re.search(r"(?<![\d.,])346(?![\d]|[.,]\d)", txt), f"la respuesta no da 346: {txt[:300]!r}"

pasos = auditoria()
if pasos is not None:
    pulsados = sum(p["tool"] == "ui_act" and p["ok"] and p["rung"] == "2" and p["via"] == "atspi"
                   and p["resource_class"] == "org.kde.kcalc" for p in pasos)
    assert pulsados >= 8, f"solo {pulsados} pulsaciones por AT-SPI en KCalc: la cuenta no salió de sus botones"

print("kcalc-cuenta-09: OK · 346")
