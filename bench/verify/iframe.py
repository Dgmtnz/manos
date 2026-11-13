"""¿Sacó del visor entre orígenes las facturas pendientes, y solo esas, a un CSV que se abre bien?
(bench/tasks/web-iframe-23.json)

El visor del 4.º trimestre es un iframe de `localhost` dentro de una página de `127.0.0.1`: otro origen, otro
proceso de renderizado, y su tabla solo enseña unas seis filas sin desplazarse dentro de él. Lo que miraría una
persona en `pendientes.csv`: la cabecera `numero,cliente,importe`, las cinco facturas en estado «Pendiente» en
el orden del visor, el importe como número con punto decimal y «Talleres Ruiz, S.L.» en una sola columna (un
CSV escrito a mano sin comillas la parte en dos). Las trampas que delata cada aserción: copiar la tabla del
archivo del 3.er trimestre, que está fuera del iframe y también tiene pendientes; quedarse con las filas
visibles sin desplazar el visor; y colar las «Vencida», que no se pidieron.

La lista esperada sale del propio fixture (`<script id="datos">` de bench/fixtures/facturas-visor.html).
"""

import csv
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "facturas-visor.html"

bloque = re.search(r'<script type="application/json" id="datos">(.*?)</script>', FIXTURE.read_text(encoding="utf-8"),
                   re.S)
esperado = [(f["numero"], f["cliente"], Decimal(f["importe"])) for f in json.loads(bloque.group(1))
            if f["estado"] == "Pendiente"]
assert len(esperado) == 5, f"el fixture cambió: {len(esperado)} pendientes"

ruta = HOME / "pendientes.csv"
assert ruta.exists(), "no existe pendientes.csv en el directorio de trabajo"
with open(ruta, newline="", encoding="utf-8-sig") as f:
    filas = [fila for fila in csv.reader(f) if fila]
assert filas, "pendientes.csv está vacío"
assert [c.strip() for c in filas[0]] == ["numero", "cliente", "importe"], f"cabecera: {filas[0]}"

puesto = []
for n, fila in enumerate(filas[1:], 2):
    assert len(fila) == 3, f"línea {n}: {len(fila)} columnas en vez de 3 — {fila} (¿una coma sin comillas?)"
    numero, cliente, importe = (c.strip() for c in fila)
    try:
        puesto.append((numero, cliente, Decimal(importe)))
    except InvalidOperation:
        raise AssertionError(f"línea {n}: importe {importe!r} no es un número con punto decimal") from None

numeros = [p[0] for p in puesto]
archivo = [x for x in numeros if x.startswith("F-2026-03")]
assert not archivo, f"{archivo} son del archivo del 3.er trimestre, fuera del visor"
faltan = [e[0] for e in esperado if e[0] not in numeros]
assert not faltan, f"faltan {faltan} (¿no se desplazó dentro del visor?)"
sobran = [x for x in numeros if x not in {e[0] for e in esperado}]
assert not sobran, f"{sobran} no están pendientes (¿se colaron las vencidas?)"
assert puesto == esperado, f"las filas no cuadran con el visor:\n  puesto   {puesto}\n  esperado {esperado}"

print(f"web-iframe-23: OK · {len(puesto)} pendientes del visor de otro origen")
