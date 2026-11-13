"""¿Pasó las 120 ventas de la API a NDJSON, byte a byte, con browser_extract?
(bench/tasks/web-extraer-30.json; con `cupo`, web-429-31.json)

Lo que miraría una persona abriendo `ventas.ndjson`: las 120 ventas de las 5 páginas de la API, en orden, una por
línea, cada una idéntica a la de la API (mismos campos en el mismo orden, mismos tipos: `importe` número, `pagado`
booleano, `nota` null). Se compara la línea entera con `json.dumps(venta, ensure_ascii=False)`, que es lo que
escribe browser_extract; una copia a mano que cambie un espacio no pasa, y es a propósito: esta tarea mide la
extracción sin pasar por el contexto. Con auditoría, que hubo un browser_extract que acabó bien.

Con `cupo`, la API dio 429 con Retry-After en las páginas 2 y 4 (`$BENCH_HOME/ventas-cupo.json`, lo apunta el
servidor): tienen que haber saltado, sin ningún reintento antes de que venciera el Retry-After (las peticiones de la
propia página no tienen cupo), y el fichero tiene que estar entero igual.
"""

import json
import sys

from comun import HOME, auditoria
from serve import ventas

esperado = [json.dumps(v, ensure_ascii=False) for v in ventas()]
ruta = HOME / "ventas.ndjson"
assert ruta.exists(), "no existe ventas.ndjson en el directorio de trabajo"
lineas = ruta.read_text(encoding="utf-8").removesuffix("\n").split("\n")
assert len(lineas) == len(esperado), f"{len(lineas)} líneas en vez de {len(esperado)} (¿paró antes, o repitió una página?)"
for n, (got, want) in enumerate(zip(lineas, esperado), 1):
    assert got == want, f"línea {n}: {got[:120]!r} en vez de {want[:120]!r}"

if "cupo" in sys.argv[1:]:
    cupo = json.loads((HOME / "ventas-cupo.json").read_text()) if (HOME / "ventas-cupo.json").exists() else {}
    assert cupo.get("429", 0) >= 2, f"la API no llegó a dar los 429 de las páginas 2 y 4: {cupo}"
    assert cupo["early"] == 0, f"{cupo['early']} reintentos antes de que venciera el Retry-After: {cupo}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "browser_extract" and p["ok"] for p in pasos), "no hubo ningún browser_extract que acabara bien"

print(f"{'web-429-31' if 'cupo' in sys.argv[1:] else 'web-extraer-30'}: OK · {len(lineas)} ventas idénticas a la API")
