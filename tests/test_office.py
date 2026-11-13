"""Autocomprobación de office.py con LibreOffice sin cabeza de verdad (~10 s):  uv run --project . tests/test_office.py

Hoja nueva con fórmulas (también con comas) y la relectura recalculada; leer .xlsx, .csv, .docx con tabla y PDF;
convertir; no pisar otro fichero sin approved; rutas relativas y vedadas; lectura grande a muestra y a fichero;
un trabajador o un soffice muertos se reemplazan; `stop()` o un servidor muerto con SIGKILL no dejan
soffice huérfano; y las tools por el protocolo.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tmp = Path(tempfile.mkdtemp(prefix="manos-office-"))
os.environ["XDG_DATA_HOME"] = str(tmp / "data")

import office
import server
from browser.forms import PathRefused
from desktop.apps import NeedsApproval


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return str(e) or type(e).__name__
    return ""


def docx(path: Path) -> None:
    """Un .docx mínimo: un párrafo y una tabla de 2x2."""
    p = lambda t: f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"
    cell = lambda t: f"<w:tc>{p(t)}</w:tc>"
    body = p("Informe de prueba") + "<w:tbl>" + "".join(
        f"<w:tr>{cell(a)}{cell(b)}</w:tr>" for a, b in (("Mes", "Total"), ("Enero", "1234"))) + "</w:tbl>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats'
                   '-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                   f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')


x = tmp / "suma.xlsx"
edits = [*({"cell": f"A{i}", "value": 10 * i} for i in range(1, 6)), {"cell": "a6", "formula": "=SUM(A1:A5)"},
         {"cell": "B1", "formula": '=IF(A1>5,"si, mucho",2)'}, {"cell": "B2", "value": "10"},
         {"cell": "B3", "formula": "=1/0"}]
r = office.write(str(x), edits, "", "", False)
assert r["created"] and x.exists(), r
assert "A6 150 =SUM(A1:A5)" in r["lines"], r["lines"]
assert 'B1 "si, mucho" =IF(A1>5;"si, mucho";2)' in r["lines"], r["lines"]  # la coma de dentro del texto se queda
assert 'B2 "10"' in r["lines"] and r["lines"][-1].startswith("B3 Err:532"), r["lines"]
pid = office._office
assert pid and os.path.exists(f"/proc/{pid}")
assert office._profile(os.getpid()).is_dir()  # un perfil por proceso servidor

r = office.read(str(x), "", "", "")
assert r["range"] == "A1:B6" and r["total"] == 9 and not r["truncated"], r
assert "A6 150 =SUM(A1:A5)" in r["lines"] and any(l.startswith("B3 Err:532") for l in r["lines"]), r["lines"]
assert office.read(str(x), "", "A6", "")["lines"] == ["A6 150 =SUM(A1:A5)"]
assert "Hay: " in raises(ValueError, office.read, str(x), "Nope", "", "")

# editar en el sitio: .xlsx pide approved (LibreOffice puede perder cosas de Excel); save_as encima de otro, también
antes = x.read_bytes()
assert "Excel" in raises(NeedsApproval, office.write, str(x), [{"cell": "A1", "value": 11}], "", "", False)
assert x.read_bytes() == antes
assert office.write(str(x), [{"cell": "A1", "value": 11}], "", "", True)["lines"] == ["A1 11"]
assert "A6 151" in office.read(str(x), "", "A6", "")["lines"][0]
otro = tmp / "otro.ods"
otro.write_text("no tocar")
assert raises(NeedsApproval, office.write, str(x), [{"cell": "A1", "value": 1}], "", str(otro), False)
assert otro.read_text() == "no tocar"
assert office.write(str(x), [{"cell": "A1", "value": 1}], "", str(otro), True)["saved_to"] == str(otro)
assert "A6 141" in office.read(str(otro), "", "A6", "")["lines"][0]
assert "A6 151" in office.read(str(x), "", "A6", "")["lines"][0]  # el original, intacto

# convertir y PDF
assert office.convert(str(x), "ods", False)["saved_to"] == str(tmp / "suma.ods")
assert raises(NeedsApproval, office.convert, str(x), ".ods", False)  # ya existe
assert office.write(str(tmp / "suma.ods"), [{"cell": "C1", "value": 1}], "", "", False)["lines"] == ["C1 1"]  # .ods sí
(tmp / "sub").mkdir()
pdf = office.convert(str(tmp / "suma.ods"), str(tmp / "sub" / "x.pdf"), False)
assert pdf["bytes"] > 1000, pdf
r = office.read(pdf["saved_to"], "", "", "")
assert any("151" in l for l in r["lines"]) and r["sheets"] == [], r
assert raises(ValueError, office.convert, str(x), "docx", False)
assert "mismo fichero" in raises(ValueError, office.convert, str(x), "XLSX", False)
assert raises(ValueError, office.read, pdf["saved_to"], "", "A1", "")

# csv: leer; en el sitio nunca (08001 → 8001), con save_as a uno nuevo sí
c = tmp / "datos.csv"
c.write_text("nombre,importe\nana,10\nluis,32.5\n")
assert office.read(str(c), "", "", "")["lines"] == ['A1 "nombre"', 'B1 "importe"', 'A2 "ana"', 'B2 "10"',
                                                    'A3 "luis"', 'B3 "32.5"']
cp = tmp / "cp.csv"
cp.write_text("08001,1234567890123456789\n")
# leer un CSV no adivina tipos: el cero inicial y el id de 19 cifras salen enteros
r = office.read(str(cp), "", "", "")
assert r["lines"] == ['A1 "08001"', 'B1 "1234567890123456789"'] and r["range"] == "A1:B1", r
assert office.read(str(c), "", "B2:B3", "")["lines"] == ['B2 "10"', 'B3 "32.5"']
for e in ({"cell": "B4", "formula": "=SUM(B2:B3)"}, {"cell": "C1", "value": 1}):
    assert raises(office.LossyInPlace, office.write, str(c), [e], "", "", True)  # ni con approved
    assert raises(office.LossyInPlace, office.write, str(cp), [e], "", str(cp), True)  # save_as al mismo
assert cp.read_text() == "08001,1234567890123456789\n" and c.read_text().startswith("nombre,importe\nana,10")
assert "save_as" in office.LossyInPlace.next
office.write(str(c), [{"cell": "B4", "formula": "=SUM(B2:B3)"}], "", str(tmp / "datos2.csv"), False)
assert (tmp / "datos2.csv").read_text().splitlines()[-1] == ",42.5"

# docx con tabla: la tabla sale en el texto
d = tmp / "informe.docx"
docx(d)
lines = office.read(str(d), "", "", "")["lines"]
assert lines == ["Informe de prueba", "Mes\tTotal", "Enero\t1234"], lines
assert raises(ValueError, office.read, str(d), "", "A1", "")
assert office.convert(str(d), "pdf", False)["bytes"] > 1000

# rutas
assert "absoluta" in raises(ValueError, office.read, "suma.xlsx", "", "", "")
assert raises(PathRefused, office.write, str(Path.home() / ".manos-test" / "x.ods"), edits, "", "", False)
assert raises(PathRefused, office.read, str(x), "", "", str(Path.home() / ".manos-test.txt"))
assert "no existe" in raises(ValueError, office.read, str(tmp / "nota.txt"), "", "", "")
(tmp / "nota.txt").write_text("x")
assert "no vale aquí" in raises(ValueError, office.read, str(tmp / "nota.txt"), "", "", "")
for bad in ({"cell": "A1"}, {"cell": "A1", "value": 1, "formula": "=1"}, {"cell": "A1", "formula": "SUM(A1)"},
            {"cell": "A1", "value": True}, {"cell": "A1:A2", "value": 1}):
    assert raises(ValueError, office.write, str(x), [bad], "", "", True), bad
assert "matriz" in raises(ValueError, office.write, str(x), [{"cell": "A1", "formula": "=SUM({1,2;3,4})"}], "", "",
                          True)

# lectura grande: muestra, o todo a fichero
big = tmp / "big.csv"
big.write_text("".join(f"{i},{i * 2}\n" for i in range(300)))
r = office.read(str(big), "", "", "")
assert r["truncated"] and r["total"] == 600 and len(r["lines"]) == office.SAMPLE, r["total"]
dump = tmp / "big.txt"
r = office.read(str(big), "", "", str(dump))
assert r["saved_to"] == str(dump) and len(dump.read_text().splitlines()) == 600 and len(r["lines"]) == office.SAMPLE
# el volcado nunca pisa: ni otro fichero, ni el documento, ni un enlace duro a él
assert raises(office.DumpExists, office.read, str(big), "", "", str(dump))
assert raises(office.DumpExists, office.read, str(big), "", "", str(big))
os.link(big, tmp / "duro.csv")
antes = big.read_bytes()
assert raises(office.DumpExists, office.read, str(big), "", "", str(tmp / "duro.csv"))
assert big.read_bytes() == antes and "otro `filename`" in office.DumpExists.next

# un trabajador muerto se reemplaza, y solo entonces el servidor mata el grupo de soffice (killpg)
real_killpg, grupos = os.killpg, []
os.killpg = lambda g, sig: (grupos.append(g), real_killpg(g, sig))[1]
office._proc.kill()
office._proc.wait()
assert office.read(str(x), "", "A6", "")["lines"] == ["A6 151 =SUM(A1:A5)"]
assert grupos == [pid], grupos
os.killpg = real_killpg
assert not os.path.exists(f"/proc/{pid}") and office._office != pid
pid = office._office
os.killpg(pid, 9)  # soffice se cae: falla esa llamada, la siguiente arranca otro
assert "se cayó" in raises(office.OfficeError, office.read, str(x), "", "A6", "")
assert office.read(str(x), "", "A6", "")["lines"] == ["A6 151 =SUM(A1:A5)"]
assert not os.path.exists(f"/proc/{pid}") and office._office != pid

# otro servidor a la vez: su propio perfil y su propio soffice; muere con SIGKILL sin dejar soffice huérfano, y el
# nuestro sigue sirviendo
hijo = subprocess.run([sys.executable, "-c", "import os, sys; sys.path.insert(0, '.'); import office; "
                       f"office.read({str(x)!r}, '', 'A6', ''); print(os.getpid(), office._office, flush=True); "
                       "os.kill(os.getpid(), 9)"],
                      cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
hpid, otro_soffice = map(int, hijo.stdout.split())
assert otro_soffice != office._office
for _ in range(50):
    if not os.path.exists(f"/proc/{otro_soffice}"):
        break
    time.sleep(0.2)
assert not os.path.exists(f"/proc/{otro_soffice}"), f"soffice {otro_soffice} sobrevivió al servidor"
assert office.read(str(x), "", "A6", "")["lines"] == ["A6 151 =SUM(A1:A5)"]
assert office._profile(hpid).is_dir()  # el muerto no pasó por stop(): su perfil se barre al arrancar otro

# stop() normal: el trabajador cierra soffice, el servidor no hace killpg, y el perfil se borra
pid, grupos = office._office, []
os.killpg = lambda g, sig: (grupos.append(g), real_killpg(g, sig))[1]
office.stop()
os.killpg = real_killpg
assert grupos == [] and not os.path.exists(f"/proc/{pid}") and office._proc is None
assert not office._profile(os.getpid()).exists()


# por el protocolo: observation_id, error tipado con su `next`
async def proto():
    from mcp import Client
    async with Client(server.mcp) as cl:
        r = (await cl.call_tool("office_read", {"path": str(x), "range": "A6"})).structured_content
        assert r["ok"] and r["lines"] == ["A6 151 =SUM(A1:A5)"] and r["observation_id"].startswith("obs-"), r
        r = (await cl.call_tool("office_write", {"path": str(x), "edits": [{"cell": "A1", "value": 1}],
                                                 "save_as": str(otro)})).structured_content
        assert not r["ok"] and r["error"]["type"] == "NeedsApproval" and "approved=true" in r["error"]["next"], r


asyncio.run(proto())
assert not office._profile(hpid).exists()
office.stop()
assert not any(tmp.glob(".~lock*")), list(tmp.iterdir())  # LibreOffice no deja candados
print("test_office: OK")
