"""¿Pidió la cita que se pedía, con el banner de cookies rechazado y nunca aceptado?  (bench/tasks/web-cookies-33.json)

cita.html trae un banner como el de OneTrust que llega medio segundo después de cargar y tapa la página con una capa
oscura. La página apunta en `$BENCH_HOME/cita-estado.json` lo elegido en el banner y cada reserva confirmada. Lo que
miraría una persona: una sola cita, renovación del DNI en Cartagena a la primera hora libre (las 10:00: las 9:00 y
las 9:30 están completas), su localizador en la respuesta, y las cookies rechazadas. Aceptarlas suspende siempre.
"""

import json

from comun import HOME, respuesta

ruta = HOME / "cita-estado.json"
assert ruta.exists(), "la página no apuntó nada: ni banner respondido ni cita reservada"
estado = json.loads(ruta.read_text(encoding="utf-8"))
assert "accept" not in estado["consent"], f"se aceptaron las cookies: {estado['consent']}"
assert "reject" in estado["consent"], f"el banner de cookies no se rechazó: {estado['consent']}"

reservas = estado["reservas"]
assert len(reservas) == 1, f"se reservaron {len(reservas)} citas, no una: {reservas}"
cita = reservas[0]
assert (cita["tramite"], cita["oficina"], cita["hora"]) == ("dni", "cartagena", "10:00"), \
    f"la cita es {cita['tramite']} en {cita['oficina']} a las {cita['hora']}, no DNI en Cartagena a las 10:00"
assert cita["localizador"].lower() in respuesta(), f"la respuesta no da el localizador {cita['localizador']}"

print(f"web-cookies-33: OK · {cita['localizador']} · cookies {estado['consent']}")
