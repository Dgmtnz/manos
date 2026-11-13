"""¿Pasó la tabla de inventario.pdf a inventario.csv, entera y sin el título ni las notas?
(bench/tasks/office-tabla-29.json)

Lo que miraría una persona al abrir el CSV en una hoja de cálculo: la cabecera de cuatro columnas y los seis
productos con su código, nombre, unidades y precio, en ese orden, sin filas de más. Vale coma o punto y coma de
separador (el precio lleva coma decimal: con coma de separador, entre comillas), y `0,08` o `0.08` en el precio.
La respuesta tiene que decir 6; con auditoría, que el PDF se leyó con office_read (sin Bash no hay otro camino).
"""

import csv
import re

from comun import HOME, auditoria, respuesta

CABECERA = ["Código", "Producto", "Unidades", "Precio unitario (€)"]
FILAS = [["AL-101", "Tornillo M6", "1200", "0,08"], ["AL-102", "Tuerca M6", "950", "0,05"],
         ["AL-215", "Arandela 6 mm", "2000", "0,02"], ["BR-007", "Broca de 8 mm", "35", "3,40"],
         ["BR-010", "Broca de 10 mm", "28", "4,15"], ["PN-300", "Pintura blanca 4 L", "12", "18,90"]]

ruta = HOME / "inventario.csv"
assert ruta.is_file(), "no hay inventario.csv en el directorio de trabajo"
texto = ruta.read_text(encoding="utf-8-sig")
sep = ";" if texto.splitlines()[0].count(";") >= 3 else ","
filas = [[c.strip() for c in f] for f in csv.reader(texto.splitlines(), delimiter=sep) if any(c.strip() for c in f)]
assert filas, "inventario.csv está vacío"
norm = lambda s: " ".join(s.split()).lower()
assert [norm(c) for c in filas[0]] == [norm(c) for c in CABECERA], f"la cabecera es {filas[0]}, no {CABECERA}"
cuerpo = filas[1:]
assert len(cuerpo) == len(FILAS), f"hay {len(cuerpo)} filas de productos, no {len(FILAS)}"
for got, want in zip(cuerpo, FILAS):
    assert len(got) == 4, f"la fila {got} no tiene 4 columnas (¿el separador partió el precio?)"
    assert got[:3] == want[:3] and got[3].replace(".", ",") == want[3], f"la fila {got} debería ser {want}"
assert re.search(r"\b(6|seis)\W{0,3}(productos|filas|referencias|articulos)", respuesta()), \
    "la respuesta no dice que hay 6 productos"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "office_read" and p["ok"] for p in pasos), "el PDF no se leyó con office_read"

print(f"office-tabla-29: OK · {len(cuerpo)} productos, separador {sep!r}")
