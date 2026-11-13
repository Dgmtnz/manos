"""¿Esperó a que llegara el XHR y dio el punto y el código de verdad, no los guardados?
(bench/tasks/web-xhr-26.json)

La página de seguimiento pinta primero los datos guardados de ayer (Librería Arco, código 3391-KD) con un
indicador de «actualizando…», y a los 2 s los sustituye por la respuesta de un XHR a /envio-datos.json, que es
el único sitio donde están los buenos. Lo que miraría una persona: que la respuesta da el punto y el código
del XHR y no da como resultado el código caducado (citarlo después para descartarlo vale), que la página llegó a pintar los datos nuevos (ella misma apunta la
hora en `$BENCH_HOME/envio-xhr.json`) y, con auditoría, que hubo un browser_observe **posterior** a esa hora:
responder con la primera observación es el fallo que esta tarea busca.
"""

import json
from pathlib import Path

from comun import HOME, auditoria, principal, respuesta

datos = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "envio-datos.json").read_text(encoding="utf-8"))
CODIGO, PUNTO, VIEJO = datos["codigo"].lower(), "estanco plaza mayor", "3391-kd"

ruta = HOME / "envio-xhr.json"
assert ruta.exists(), "la página no llegó a pintar los datos del XHR (¿no se abrió, o se cerró antes de 2 s?)"
cargado = json.loads(ruta.read_text(encoding="utf-8"))["cargado"]

txt = respuesta()
assert VIEJO not in principal(txt), f"la respuesta da como bueno el código guardado {VIEJO.upper()}, que ya no vale: {txt[:300]!r}"
assert CODIGO in txt, f"la respuesta no da el código {CODIGO.upper()}: {txt[:300]!r}"
assert PUNTO in txt, f"la respuesta no da el punto de recogida (Estanco Plaza Mayor): {txt[:300]!r}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "browser_observe" and p["ok"] and p["ts"] >= cargado for p in pasos), \
        "ninguna observación después de que llegara el XHR: se respondió con la primera"

print(f"web-xhr-26: OK · {CODIGO.upper()} en {PUNTO}, observado después del XHR")
