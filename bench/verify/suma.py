"""¿Hay una suma.xlsx con 10..50 en A1:A5 y en A6 una fórmula que da 150?  (bench/tasks/office-suma-27.json)

Lo que miraría una persona al abrirla en Excel: los cinco números en su sitio y, en A6, 150 calculado por una
fórmula que mira A1:A5 (no el 150 escrito a mano). Se lee el .xlsx con zipfile (el venv no tiene `uno`): la hoja
primera, cada `<c r=…>` con su `<v>` y su `<f>`. El valor guardado de A6 es el que verá quien la abra sin
recalcular. La respuesta tiene que dar 150; con auditoría, que la hoja salió de office_write.
"""

import re
import zipfile
import xml.etree.ElementTree as ET

from comun import HOME, auditoria, respuesta

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

ruta = HOME / "suma.xlsx"
assert ruta.is_file(), "no hay suma.xlsx en el directorio de trabajo"
assert zipfile.is_zipfile(ruta), "suma.xlsx no es un .xlsx de verdad (no es un zip)"
with zipfile.ZipFile(ruta) as z:
    hojas = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    assert hojas, "el .xlsx no tiene ninguna hoja"
    raiz = ET.fromstring(z.read(hojas[0]))
celdas = {c.get("r"): c for c in raiz.iterfind(".//m:c", NS)}


def hijo(ref: str, tag: str):
    """El `<v>` o el `<f>` de una celda, o None."""
    c = celdas.get(ref)
    return None if c is None else c.find(f"m:{tag}", NS)


def valor(ref: str) -> float | None:
    v = hijo(ref, "v")
    return float(v.text) if v is not None and v.text else None


for i in range(1, 6):
    assert valor(f"A{i}") == 10 * i, f"A{i} vale {valor(f'A{i}')}, no {10 * i}"
    assert hijo(f"A{i}", "f") is None, f"A{i} es una fórmula, no el número"
f = hijo("A6", "f")
assert f is not None and f.text, "A6 no tiene fórmula"
assert "A1:A5" in f.text.upper() or all(f"A{i}" in f.text.upper() for i in range(1, 6)), \
    f"la fórmula de A6 ({f.text}) no suma A1:A5"
assert valor("A6") == 150, f"A6 guarda {valor('A6')}, no 150"
assert re.search(r"\b150\b", respuesta()), "la respuesta no dice que A6 da 150"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "office_write" and p["ok"] for p in pasos), "la hoja no se escribió con office_write"

print(f"office-suma-27: OK · A6 ={f.text} → 150")
