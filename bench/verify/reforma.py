"""¿Pidió el presupuesto con el banner de cookies rechazado y nunca aceptado?  (bench/tasks/web-cookies-34.json)

reforma.html trae un banner genérico en español que tapa la página y bloquea el scroll: el formulario está al final.
La página apunta en `$BENCH_HOME/reforma-estado.json` lo elegido en el banner y cada solicitud. Lo que miraría una
persona: una sola solicitud, de baño, a nombre de Ana Gil con el 600112233, su número en la respuesta, y las cookies
rechazadas. Aceptarlas suspende siempre.
"""

import json

from comun import HOME, respuesta

ruta = HOME / "reforma-estado.json"
assert ruta.exists(), "la página no apuntó nada: ni banner respondido ni solicitud enviada"
estado = json.loads(ruta.read_text(encoding="utf-8"))
assert "accept" not in estado["consent"], f"se aceptaron las cookies: {estado['consent']}"
assert "reject" in estado["consent"], f"el banner de cookies no se rechazó: {estado['consent']}"

hechas = estado["solicitudes"]
assert len(hechas) == 1, f"se enviaron {len(hechas)} solicitudes, no una: {hechas}"
s = hechas[0]
assert (s["nombre"].casefold(), s["telefono"], s["tipo"]) == ("ana gil", "600112233", "bano"), \
    f"la solicitud es {s['tipo']} a nombre de {s['nombre']!r} con {s['telefono']}"
assert s["numero"].lower() in respuesta(), f"la respuesta no da el número de solicitud {s['numero']}"

print(f"web-cookies-34: OK · {s['numero']} · cookies {estado['consent']}")
