"""¿Está la ventana del formulario donde se pidió?  (bench/tasks/ventana-colocar-11.json)

Una persona lo vería de un vistazo; aquí la KWin anidada puede haber muerto ya, así que se juntan dos
testigos. El tamaño lo apunta el propio formulario (`alta.json`, bench/fixtures/alta.py): en Wayland un
cliente sabe su tamaño pero no su posición. Con el marco de 720×520 el cliente mide 720 de ancho y 520 menos
la barra de título (36 px medidos en la anidada; se acepta 20-48). La posición sale de la respuesta: tiene que
dar los cuatro números que releyó `window_set`, y con auditoría, que hubo un `window_set` acertado en esa ventana.
"""

import json
import re

from comun import HOME, auditoria, respuesta

X, Y, ANCHO, ALTO = 100, 80, 720, 520

estado = HOME / "alta.json"
assert estado.exists(), "el formulario no llegó a abrirse"
ancho, alto = json.loads(estado.read_text(encoding="utf-8"))["size"]
assert ancho == ANCHO and ALTO - 48 <= alto <= ALTO - 20, \
    f"el cliente mide {ancho}×{alto}: no es un marco de {ANCHO}×{ALTO}"

numeros = set(re.findall(r"\d+", respuesta()))
falta = [v for v in (X, Y, ANCHO, ALTO) if str(v) not in numeros]
assert not falta, f"la respuesta no da la geometría que quedó (faltan {falta})"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "window_set" and p["ok"] and p["resource_class"] == "manos-alta" for p in pasos), \
        "ningún window_set acertado en la ventana del formulario"

print(f"ventana-colocar-11: OK · cliente {ancho}×{alto}")
