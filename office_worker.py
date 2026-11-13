"""Trabajador de LibreOffice de manos: el único proceso que importa `uno`.

`uno` solo está en el Python del sistema, así que esto corre en `/usr/bin/python3 -I` (como
desktop/atspi_worker.py), sin importar nada de manos y hablando JSON por líneas con `office.py`. Arranca su
propio soffice sin cabeza, con perfil privado (`-env:UserInstallation`: sin él pelea por el candado de
instancia única con el LibreOffice de Diego) y por tubería, sin puerto TCP. soffice va en su propia sesión y
se mata por grupo al acabar: al cerrarse stdin, con SIGTERM o si el servidor muere (aunque sea con SIGKILL y
con una llamada colgada: lo vigila un hilo por el pid del padre).

Uso:  /usr/bin/python3 -I office_worker.py --pipe <nombre> --profile <file:// del perfil>
Primera línea:  {"soffice": <pid>}  cuando la conexión está lista, o {"error": ..., "msg": ...}.
Peticiones:  {"op": "read", path, sheet, range}      → {"sheets", "sheet", "range", "lines"} o {"text"}
             {"op": "write", path, new, edits, sheet, target}  → {"sheet", "lines"}   (edits ya validados)
             {"op": "convert", path, target}         → {"target"}
Respuestas:  el resultado, o {"error": <tipo>, "msg": <texto para el modelo>}. Con `OfficeDown` (soffice murió) el
             trabajador sale tras contestar.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

uno = None  # se carga en main()
MAX_CELLS = 200_000  # ponytail: getDataArray entero en memoria; para hojas mayores, leer por bloques
CSV = "44,34,76,1"  # coma, comillas dobles, UTF-8, desde la fila 1
# filtro de exportación por extensión, según el tipo de documento
FILTERS = {
    "calc": {".ods": "calc8", ".xlsx": "Calc MS Excel 2007 XML", ".csv": "Text - txt - csv (StarCalc)",
             ".pdf": "calc_pdf_Export"},
    "writer": {".odt": "writer8", ".docx": "MS Word 2007 XML", ".pdf": "writer_pdf_Export"},
}


class Err(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


def _props(**kw):
    from com.sun.star.beans import PropertyValue
    out = []
    for k, v in kw.items():
        p = PropertyValue()
        p.Name, p.Value = k, v
        out.append(p)
    return tuple(out)


def _col(c: int) -> str:
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def _load(desktop, path: str, readonly: bool):
    ext = os.path.splitext(path)[1].lower()
    # sin macros ni enlaces externos: el documento puede venir de cualquier sitio
    kw = dict(Hidden=True, ReadOnly=readonly, MacroExecutionMode=0, UpdateDocMode=0)
    if ext == ".csv":
        kw |= dict(FilterName=FILTERS["calc"][".csv"], FilterOptions=CSV)
    doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(path), "_blank", 0, _props(**kw))
    if doc is None:
        raise Err("OfficeError", f"LibreOffice no pudo abrir {os.path.basename(path)}.")
    return doc


def _kind(doc) -> str:
    if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return "calc"
    if doc.supportsService("com.sun.star.text.TextDocument"):
        return "writer"
    return "otro"


def _store(doc, target: str) -> None:
    ext = os.path.splitext(target)[1].lower()
    kind = _kind(doc)
    flt = FILTERS.get(kind, {}).get(ext)
    if not flt:
        raise Err("ValueError", f"Un documento de tipo {kind} no se guarda como {ext or 'sin extensión'}: "
                                f"vale {', '.join(FILTERS.get(kind, {})) or 'nada'}.")
    kw = {"FilterName": flt, "Overwrite": True}
    if ext == ".csv":
        kw["FilterOptions"] = CSV
    doc.storeToURL(uno.systemPathToFileUrl(target), _props(**kw))


def _sheet(doc, name: str):
    sheets = doc.getSheets()
    if not name:
        return sheets.getByIndex(0)
    if not sheets.hasByName(name):
        raise Err("ValueError", f"No hay hoja '{name}'. Hay: {', '.join(sheets.getElementNames())}.")
    return sheets.getByName(name)


def _num(v: float) -> str:
    return f"{v:.15g}"


def _semis(f: str) -> str:
    """`=IF(A1>5,1,2)` → `=IF(A1>5;1;2)`: la API solo entiende `;`, y una coma fuera de comillas no significa nada
    en su gramática (los decimales van con punto), así que se cambia sin mirar más."""
    out, q = [], None
    for ch in f:
        if q:
            q = None if ch == q else q
        elif ch in "\"'":
            q = ch
        elif ch == ",":
            ch = ";"
        out.append(ch)
    return "".join(out)


def _cell_line(ref: str, data, formula: str, cell=None) -> str | None:
    """`A1 10`, `B1 "texto"`, `A6 150 =SUM(A1:A5)`, `B4 Err:532 =1/0`; None si está vacía."""
    if data == "" and formula == "":
        return None
    if data is None:  # error de fórmula: getDataArray no trae el código
        val = f"Err:{cell.getError()}"
    else:
        val = _num(data) if isinstance(data, float) else json.dumps(data, ensure_ascii=False)
    return f"{ref} {val} {formula}" if formula.startswith("=") else f"{ref} {val}"


def _lines(rng) -> list[str]:
    a = rng.getRangeAddress()
    data, forms = rng.getDataArray(), rng.getFormulaArray()
    out = []
    for r, (drow, frow) in enumerate(zip(data, forms)):
        for c, (d, f) in enumerate(zip(drow, frow)):
            ref = f"{_col(a.StartColumn + c)}{a.StartRow + r + 1}"
            if (line := _cell_line(ref, d, f, d is None and rng.getCellByPosition(c, r))) is not None:
                out.append(line)
    return out


def _cell(sheet, ref: str):
    try:
        rng = sheet.getCellRangeByName(ref)
    except Exception:
        raise Err("ValueError", f"'{ref}' no es una celda (A1, B12…).") from None
    a = rng.getRangeAddress()
    if a.StartColumn != a.EndColumn or a.StartRow != a.EndRow:
        raise Err("ValueError", f"'{ref}' es un rango: cada edición va a una sola celda.")
    return rng.getCellByPosition(0, 0)


def _readback(sheet, ref: str) -> str:
    return (_lines(_cell(sheet, ref)) or [f"{ref} (vacía)"])[0]


def read(desktop, req):
    doc = _load(desktop, req["path"], True)
    try:
        kind = _kind(doc)
        if kind == "writer":
            if req.get("sheet") or req.get("range"):
                raise Err("ValueError", "`sheet` y `range` son de hojas de cálculo; esto es un texto.")
            # getText().getString() no trae las tablas: por párrafos, y cada tabla por filas con tabuladores
            out = []
            for el in doc.getText().createEnumeration():
                if el.supportsService("com.sun.star.text.TextTable"):
                    out += ["\t".join(_num(v) if isinstance(v, float) else v for v in row) for row in el.getDataArray()]
                else:
                    out.append(el.getString())
            return {"text": "\n".join(out)}
        if kind != "calc":
            raise Err("ValueError", "office_read lee hojas de cálculo, textos y PDF; esto no es ninguno.")
        sheet = _sheet(doc, req.get("sheet", ""))
        if req.get("range"):
            try:
                rng = sheet.getCellRangeByName(req["range"])
            except Exception:
                raise Err("ValueError", f"'{req['range']}' no es un rango (A1:C10).") from None
        else:
            cur = sheet.createCursor()
            cur.gotoEndOfUsedArea(False)
            e = cur.getRangeAddress()
            rng = sheet.getCellRangeByPosition(0, 0, e.EndColumn, e.EndRow)
        a = rng.getRangeAddress()
        if (a.EndColumn - a.StartColumn + 1) * (a.EndRow - a.StartRow + 1) > MAX_CELLS:
            raise Err("ValueError", f"Más de {MAX_CELLS} celdas: pide un `range` más pequeño.")
        name = f"{_col(a.StartColumn)}{a.StartRow + 1}:{_col(a.EndColumn)}{a.EndRow + 1}"
        return {"sheets": list(doc.getSheets().getElementNames()), "sheet": sheet.getName(), "range": name,
                "lines": _lines(rng)}
    finally:
        doc.close(True)


def write(desktop, req):
    if req["new"]:
        doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, _props(Hidden=True))
    else:
        doc = _load(desktop, req["path"], False)
    try:
        if _kind(doc) != "calc":
            raise Err("ValueError", "office_write edita hojas de cálculo; un texto se escribe con Write.")
        sheet = _sheet(doc, req.get("sheet", ""))
        for e in req["edits"]:
            cell = _cell(sheet, e["cell"])
            if "formula" in e:
                cell.setFormula(_semis(e["formula"]))
            elif isinstance(e["value"], str):
                cell.setString(e["value"])
            else:
                cell.setValue(e["value"])
        doc.calculateAll()
        lines = [_readback(sheet, e["cell"].upper()) for e in req["edits"]]
        _store(doc, req["target"])
        return {"sheet": sheet.getName(), "lines": lines}
    finally:
        doc.close(True)


def convert(desktop, req):
    doc = _load(desktop, req["path"], True)
    try:
        _store(doc, req["target"])
        return {"target": req["target"]}
    finally:
        doc.close(True)


def main():
    global uno
    pipe = sys.argv[sys.argv.index("--pipe") + 1]
    profile = sys.argv[sys.argv.index("--profile") + 1]
    responses = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())  # lo que imprima LibreOffice no ensucia la tubería

    def say(obj):
        responses.write(json.dumps(obj, ensure_ascii=False) + "\n")
        responses.flush()

    office = subprocess.Popen(["soffice", "--headless", "--invisible", "--norestore", "--nologo", "--nodefault",
                               "--nolockcheck", f"-env:UserInstallation={profile}",
                               f"--accept=pipe,name={pipe};urp;StarOffice.ComponentContext"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)

    def bye(*_):
        try:
            os.killpg(office.pid, signal.SIGTERM)  # su grupo: el guion soffice, oosplash y soffice.bin
            office.wait(5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(office.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    parent = os.getppid()

    def orphan():  # el servidor murió con una llamada colgada: stdin no se lee hasta que vuelva, y no vuelve
        while os.getppid() == parent:
            time.sleep(1)
        bye()

    threading.Thread(target=orphan, daemon=True).start()
    try:
        import uno as _uno
        uno = _uno
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        deadline = time.monotonic() + 40  # el primer arranque crea el perfil
        while True:
            try:
                ctx = resolver.resolve(f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext")
                break
            except Exception:
                if time.monotonic() > deadline or office.poll() is not None:
                    raise
                time.sleep(0.3)
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    except Exception as e:
        say({"error": "OfficeError", "msg": f"LibreOffice no arrancó ({type(e).__name__}: {e})."})
        bye()
    say({"soffice": office.pid})
    ops = {"read": read, "write": write, "convert": convert}
    for line in sys.stdin:
        try:
            req = json.loads(line)
            resp = ops[req["op"]](desktop, req)
        except Err as e:
            resp = {"error": e.kind, "msg": str(e)}
        except Exception as e:
            resp = {"error": "OfficeError", "msg": f"{type(e).__name__}: {e}"}
            if office.poll() is not None:  # soffice murió: este trabajador ya no sirve, el servidor arranca otro
                resp = {"error": "OfficeDown", "msg": resp["msg"] + ". LibreOffice se cayó; la siguiente llamada lo "
                                                                  "arranca de nuevo."}
                say(resp)
                bye()
        say(resp)
    bye()


if __name__ == "__main__":
    main()
