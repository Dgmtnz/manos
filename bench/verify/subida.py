"""¿Subió el justificante por el `<input type=file>` oculto correcto y llegaron los bytes exactos?
(bench/tasks/web-subida-25.json)

La página tiene dos selectores de fichero ocultos detrás de botones con estilo: «Añadir foto del DNI», el
primero y el señuelo, que solo admite imágenes, y «Subir justificante», el que se pidió. Lo que miraría quien
recibe la reclamación: un solo envío confirmado, con `justificante-pago.pdf` y byte a byte el mismo fichero que
había en el directorio de trabajo, y nada intentado por el campo de la foto. La página se apunta a sí misma en
`$BENCH_HOME/justificante-envio.json` y manda los bytes en base64 (bench/fixtures/justificante.html).

Los bytes se comparan con la copia de bench/fixtures, no con la de `$BENCH_HOME`: un agente que reescribiera
el fichero del directorio de trabajo no puede aprobar subiendo lo que él mismo escribió.
"""

import base64
import json
from pathlib import Path

from comun import HOME

ORIGINAL = (Path(__file__).resolve().parents[1] / "fixtures" / "justificante-pago.pdf").read_bytes()

ruta = HOME / "justificante-envio.json"
assert ruta.exists(), "no se confirmó ningún envío (la página solo se apunta al confirmar o al tocar la foto)"
st = json.loads(ruta.read_text(encoding="utf-8"))
assert not st["foto_intentos"], f"se intentó subir {st['foto_intentos']} por el campo de la foto (solo imágenes)"
envios = st["envios"]
assert envios, "se eligió el fichero pero no se confirmó el envío"
assert len(envios) == 1, f"se confirmó {len(envios)} veces"
envio = envios[0]
assert envio["nombre"] == "justificante-pago.pdf", f"se subió {envio['nombre']!r}, no justificante-pago.pdf"
recibido = base64.b64decode(envio["b64"])
assert recibido == ORIGINAL, f"llegaron {len(recibido)} bytes distintos de los {len(ORIGINAL)} del justificante"

print(f"web-subida-25: OK · {len(recibido)} bytes idénticos, la foto sin tocar")
