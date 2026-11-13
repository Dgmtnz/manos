"""¿Está presupuesto.pdf junto a la hoja, con el presupuesto dentro, y la hoja sin tocar?
(bench/tasks/office-pdf-28.json)

Lo que miraría una persona: abre el PDF y ve los cuatro conceptos con sus importes y el total, 1397; y la hoja
original sigue igual (byte a byte la de bench/fixtures). El texto del PDF sale de `pdftotext -layout`, como lo
lee office_read. La respuesta tiene que dar el total; con auditoría, que el PDF salió de office_convert.
"""

import re
import subprocess
from pathlib import Path

from comun import HOME, auditoria, respuesta

ORIGINAL = (Path(__file__).resolve().parents[1] / "fixtures" / "presupuesto.ods").read_bytes()

pdf = HOME / "presupuesto.pdf"
assert pdf.is_file(), "no hay presupuesto.pdf junto a la hoja"
assert pdf.read_bytes()[:5] == b"%PDF-", "presupuesto.pdf no es un PDF"
assert pdf.stat().st_mtime >= float((HOME / ".t0").read_text()), "presupuesto.pdf es de antes de la tarea"
texto = subprocess.run(["/usr/bin/pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True,
                       check=True).stdout
for concepto, euros in (("Encimera", 640), ("Fregadero", 185), ("Grifo", 92), ("Montaje", 480), ("Total", 1397)):
    assert re.search(rf"{concepto}\s+{euros}\b", texto), f"el PDF no muestra «{concepto} {euros}»"
assert (HOME / "presupuesto.ods").read_bytes() == ORIGINAL, "la hoja original cambió"
assert re.search(r"1\.?397", respuesta()), "la respuesta no da el total (1397)"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "office_convert" and p["ok"] for p in pasos), "el PDF no salió de office_convert"

print(f"office-pdf-28: OK · {pdf.stat().st_size} bytes, total 1397")
