"""¿Pasó las 5 páginas de socios a CSV, con la cabecera y las celdas tal como se ven?  (bench/tasks/web-tabla-32.json)

Lo que miraría una persona abriendo `socios.csv` en una hoja de cálculo: primera fila la cabecera de la tabla
(Nº socio, Nombre, Localidad, Alta, Cuota) y luego los 47 socios en orden, cada celda igual que en pantalla. Las
trampas: un nombre con comillas y coma (`Peña "La Tertulia", S.L.`) que un CSV a mano rompe, la página 5 con solo
7, y un «Siguiente» que enseña 300 ms una fila «Cargando…» antes de pintar la página nueva (quien lee en el acto repite
la anterior o guarda el «Cargando…»). Con
auditoría, que hubo un browser_extract que acabó bien.

La lista esperada sale del propio fixture (`<script id="datos">` de bench/fixtures/socios.html).
"""

import csv
import json
import re
from pathlib import Path

from comun import HOME, auditoria

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "socios.html"
socios = json.loads(re.search(r'id="datos">(.*?)</script>', FIXTURE.read_text(encoding="utf-8"), re.S).group(1))
assert len(socios) == 47, f"el fixture cambió: {len(socios)} socios"

ruta = HOME / "socios.csv"
assert ruta.exists(), "no existe socios.csv en el directorio de trabajo"
with open(ruta, newline="", encoding="utf-8-sig") as f:
    filas = list(csv.reader(f))
assert filas and filas[0] == list(socios[0]), f"cabecera {filas[:1]} en vez de {list(socios[0])}"
datos = filas[1:]
assert len(datos) == len(socios), f"{len(datos)} socios en vez de {len(socios)} (¿repitió o se saltó una página?)"
for n, (got, s) in enumerate(zip(datos, socios), 2):
    assert got == list(s.values()), f"fila {n}: {got} en vez de {list(s.values())}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "browser_extract" and p["ok"] for p in pasos), "no hubo ningún browser_extract que acabara bien"

print(f"web-tabla-32: OK · {len(datos)} socios con su cabecera")
