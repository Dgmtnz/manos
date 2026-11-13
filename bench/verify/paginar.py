"""¿Pasó las 12 páginas del catálogo a NDJSON, sin repetir y sin perder ninguno?  (bench/tasks/web-paginar-22.json)

Lo que miraría una persona abriendo `productos.ndjson`: un artículo por línea, en el orden del catálogo, con
exactamente `ref`, `nombre` y `precio` (número en euros, no el texto «4,21 €»). Las dos trampas del listado:
la paginación por desplazamiento enseña el último artículo de la página 6 otra vez como primero de la 7, y la
página 12 trae solo 3. Así que son 112 líneas para 113 filas vistas; 113 es el fallo de copiar sin mirar, y
110 o 111 el de parar al ver una página corta o un «Siguiente» que ya no avanza.

La lista esperada sale del propio fixture (`<script id="datos">` de bench/fixtures/catalogo.html), que es la
única copia de los datos.
"""

import json
import os
import re
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalogo.html"

bloque = re.search(r'<script type="application/json" id="datos">(.*?)</script>', FIXTURE.read_text(encoding="utf-8"),
                   re.S)
esperado = json.loads(bloque.group(1))
assert len(esperado) == 112, f"el fixture cambió: {len(esperado)} artículos"

ruta = HOME / "productos.ndjson"
assert ruta.exists(), "no existe productos.ndjson en el directorio de trabajo"
lineas = ruta.read_text(encoding="utf-8").removesuffix("\n").split("\n")

puesto = []
for n, linea in enumerate(lineas, 1):
    try:
        o = json.loads(linea)
    except ValueError:
        raise AssertionError(f"línea {n} no es JSON: {linea[:80]!r}") from None
    assert isinstance(o, dict), f"línea {n} no es un objeto: {linea[:80]!r}"
    assert set(o) == {"ref", "nombre", "precio"}, f"línea {n}: campos {sorted(o)} en vez de ref, nombre, precio"
    assert isinstance(o["precio"], (int, float)) and not isinstance(o["precio"], bool), \
        f"línea {n}: precio {o['precio']!r} no es un número"
    puesto.append(o)

refs = [o["ref"] for o in puesto]
repes = sorted({r for r in refs if refs.count(r) > 1})
assert not repes, f"artículos repetidos: {repes} (el de la página 7 sale dos veces en la web, no en el fichero)"
faltan = [e["ref"] for e in esperado if e["ref"] not in refs]
assert not faltan, f"faltan {len(faltan)} artículos, el primero {faltan[0]} (¿se paró antes de la página 12?)"
sobran = [r for r in refs if r not in {e["ref"] for e in esperado}]
assert not sobran, f"artículos que no están en el catálogo: {sobran[:5]}"
assert refs == [e["ref"] for e in esperado], "están todos, pero no en el orden del catálogo"
for o, e in zip(puesto, esperado):
    assert o == e, f"{e['ref']}: {o} en vez de {e}"

print(f"web-paginar-22: OK · {len(puesto)} artículos, sin el repetido de la página 7")
