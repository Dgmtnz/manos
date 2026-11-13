"""Ofimática sin interfaz: hojas de cálculo, textos y PDF por LibreOffice sin cabeza y `pdftotext`.

`uno` no se importa en el venv: LibreOffice vive en `office_worker.py` sobre `/usr/bin/python3 -I` (como
desktop/atspi.py), con JSON por líneas. Se arranca al primer uso y se reutiliza; si muere o tarda más de
`DEADLINE`, SIGKILL a él y a su soffice (por pid y grupo, nunca por nombre) y la siguiente llamada arranca otro.
Al salir el servidor, `stop()`; si el servidor muere sin pasar por ahí, el trabajador ve cerrarse su stdin y
cierra soffice él solo. Perfil privado por proceso servidor en `$XDG_DATA_HOME/manos/office-profile-<pid>`, borrado
en `stop()`; los de servidores muertos se barren al arrancar otro.

Las rutas pasan por la regla de manos (`browser.session.forbidden`); una lectura grande va a `filename` y al
contexto solo sale la cuenta y una muestra.

Autocomprobación:  uv run --project . tests/test_office.py
"""

import atexit
import contextlib
import csv
import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import verify
from browser.forms import PathRefused, safe_file
from browser.session import forbidden
from desktop.apps import NeedsApproval

WORKER = Path(__file__).with_name("office_worker.py")
DEADLINE = 60.0  # s por respuesta; el primer arranque crea el perfil (~3 s medido)
INLINE = 200  # líneas que caben en la respuesta; más, y sale una muestra
SAMPLE = 20
SHEETS = {".ods", ".xlsx", ".csv"}
TEXTS = {".odt", ".docx"}


class OfficeError(RuntimeError): pass


class DumpExists(FileExistsError):
    next = "Ese fichero ya existe y el volcado no pisa nada: repite con otro `filename` que no exista."


class LossyInPlace(PermissionError):
    next = "Usa save_as con un fichero nuevo (p. ej. datos-editado.csv); el original se queda como estaba."


_proc: subprocess.Popen | None = None
_office = 0  # pid (y grupo) del soffice del trabajador
_buf = b""
_lock = threading.RLock()  # trabajador, y comprobar-y-escribir un destino: dos llamadas no corren hacia la misma ruta


def _profile(pid: int) -> Path:
    """Un perfil por proceso servidor: con uno compartido, el soffice de un segundo manos se entrega al del primero."""
    return verify.data_dir() / f"office-profile-{pid}"


def stop() -> None:
    """Mata trabajador y soffice por su pid y borra el perfil. Sin trabajador, nada."""
    global _proc, _office, _buf
    with _lock:
        if _proc is not None:
            with contextlib.suppress(OSError):
                _proc.stdin.close()  # el trabajador cierra soffice y sale con 0
            with contextlib.suppress(subprocess.TimeoutExpired):
                _proc.wait(5)
            if _proc.poll() is None:
                _proc.kill()
                _proc.wait()
            _proc.stdout.close()
            # solo si el trabajador no cerró soffice él mismo: tras su killpg el grupo ya no existe y el pgid
            # podría ser de otro
            if _office and _proc.returncode != 0:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(_office, signal.SIGKILL)
            shutil.rmtree(_profile(os.getpid()), ignore_errors=True)
        _proc, _office, _buf = None, 0, b""


atexit.register(stop)


def _read() -> dict:
    # como desktop/atspi.py: una línea con plazo, EOF si el trabajador muere
    global _buf
    deadline, fd = time.monotonic() + DEADLINE, _proc.stdout.fileno()
    while b"\n" not in _buf:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise TimeoutError(f"LibreOffice no contestó en {DEADLINE:g} s")
        chunk = os.read(fd, 65536)
        if not chunk:
            raise EOFError("el trabajador de LibreOffice terminó")
        _buf += chunk
    line, _, _buf = _buf.partition(b"\n")
    return json.loads(line)


def _call(req: dict) -> dict:
    global _proc, _office
    with _lock:
        try:
            if _proc is None or _proc.poll() is not None:
                stop()
                for old in verify.data_dir().glob("office-profile-*"):  # de servidores muertos sin pasar por stop()
                    pid = old.name.rpartition("-")[2]
                    if pid.isdigit() and not os.path.exists(f"/proc/{pid}"):
                        shutil.rmtree(old, ignore_errors=True)
                profile = _profile(os.getpid())
                profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                _proc = subprocess.Popen(["/usr/bin/python3", "-I", str(WORKER), "--pipe",
                                          f"manos-office-{os.getpid()}", "--profile", profile.as_uri()],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
                                         start_new_session=True)
                hello = _read()
                if "error" in hello:  # el trabajador sale solo tras decirlo
                    raise OfficeError(hello["msg"])
                _office = hello["soffice"]
            _proc.stdin.write(json.dumps(req).encode() + b"\n")
            resp = _read()
        except (OSError, TimeoutError, EOFError, ValueError) as e:
            stop()
            raise OfficeError(f"{e}. La siguiente llamada arranca LibreOffice de nuevo; si el documento es enorme, "
                              "pide un `range` menor.") from None
        if (err := resp.get("error")) == "OfficeDown":
            stop()  # espera a que el trabajador salga: la siguiente llamada arranca otro
    if err:
        raise (ValueError if err == "ValueError" else OfficeError)(resp["msg"])
    return resp


def _out(path: str, approved: bool, src: str = "") -> str:
    """Ruta de salida: absoluta, fuera de lo vedado, con carpeta. Pisar un fichero que no es `src` pide approved."""
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        raise ValueError(f"{path}: hace falta una ruta absoluta (una relativa se resolvería contra el servidor).")
    if forbidden(p):
        raise PathRefused(f"{path} está en una carpeta vedada.")
    if not os.path.isdir(os.path.dirname(p)):
        raise ValueError(f"No existe la carpeta de {path}.")
    if os.path.exists(p) and os.path.realpath(p) != src and not approved:
        raise NeedsApproval(f"{path} ya existe y se sobrescribiría.")
    return os.path.realpath(p)


def _ext(path: str, allowed: set) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext not in allowed:
        raise ValueError(f"'{ext or 'sin extensión'}' no vale aquí: {', '.join(sorted(allowed))}.")
    return ext


def _col(c: int) -> str:
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def _csv(src: str, sheet: str, range_: str) -> dict:
    """Un CSV con el módulo csv: cada valor tal cual, como texto. LibreOffice adivina tipos al leerlo y pierde
    datos (08001 → 8001, un id de 19 cifras → 1.23456789012346E+18)."""
    if sheet:
        raise ValueError("Un CSV tiene una sola hoja: deja `sheet` vacío.")
    with open(src, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    c0, r0, c1, r1 = 0, 0, max(map(len, rows), default=1) - 1, max(len(rows), 1) - 1
    if range_:
        m = re.fullmatch(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", range_.strip().upper())
        if not m or int(m[2]) < 1 or (m[4] and int(m[4]) < 1):
            raise ValueError(f"'{range_}' no es un rango (A1:C10).")
        n = lambda letters: sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(letters))) - 1
        c0, r0 = n(m[1]), int(m[2]) - 1
        c1, r1 = (n(m[3]), int(m[4]) - 1) if m[3] else (c0, r0)
    lines = [f"{_col(c)}{r + 1} {json.dumps(v, ensure_ascii=False)}"
             for r, row in enumerate(rows[r0:r1 + 1], r0)
             for c, v in enumerate(row[c0:c1 + 1], c0) if v != ""]
    stem = os.path.splitext(os.path.basename(src))[0]
    return {"sheets": [stem], "sheet": stem, "range": f"{_col(c0)}{r0 + 1}:{_col(c1)}{r1 + 1}", "lines": lines}


def read(path: str, sheet: str, range_: str, filename: str) -> dict:
    """Celdas (`A6 150 =SUM(A1:A5)`; en un CSV, todo texto y tal cual) o texto por líneas; con más de INLINE
    líneas o con `filename`, todo va al fichero y sale una muestra."""
    src = safe_file(path)
    ext = _ext(src, SHEETS | TEXTS | {".pdf"})
    out = {"sheets": [], "sheet": "", "range": ""}
    if ext == ".pdf":
        if sheet or range_:
            raise ValueError("`sheet` y `range` son de hojas de cálculo; esto es un PDF.")
        got = subprocess.run(["/usr/bin/pdftotext", "-layout", "-enc", "UTF-8", src, "-"], capture_output=True,
                             timeout=DEADLINE)
        if got.returncode:
            raise OfficeError(f"pdftotext no pudo leerlo: {got.stderr.decode(errors='replace').strip()[:300]}")
        lines = got.stdout.decode("utf-8", "replace").replace("\f", "\n").splitlines()
    else:
        r = _csv(src, sheet, range_) if ext == ".csv" else _call({"op": "read", "path": src, "sheet": sheet,
                                                                   "range": range_})
        lines = r.pop("lines", None)
        lines = r.pop("text").splitlines() if lines is None else lines
        out |= r
    while lines and not lines[-1].strip():
        lines.pop()
    out |= {"total": len(lines), "saved_to": ""}
    if filename:
        dest = _out(filename, True)  # la existencia la decide O_EXCL, sin carrera (y sin seguir enlaces)
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise DumpExists(f"{filename} ya existe.") from None
        with open(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return out | {"lines": lines[:SAMPLE], "truncated": len(lines) > SAMPLE, "saved_to": dest}
    if len(lines) > INLINE:
        return out | {"lines": lines[:SAMPLE], "truncated": True}
    return out | {"lines": lines, "truncated": False}


def write(path: str, edits: list[dict], sheet: str, save_as: str, approved: bool) -> dict:
    """Pone las celdas, recalcula y guarda en `path` o `save_as`; devuelve las editadas releídas."""
    if not edits:
        raise ValueError("`edits` está vacío: pasa al menos una {cell, value} o {cell, formula}.")
    for e in edits:
        if not isinstance(e, dict) or not isinstance(e.get("cell"), str) or ("value" in e) == ("formula" in e):
            raise ValueError(f"Cada edición es {{cell, value}} o {{cell, formula}}, una de las dos: "
                             f"no vale {e!r:.120}.")
        if "formula" in e and not (isinstance(e["formula"], str) and e["formula"].startswith("=")):
            raise ValueError(f"La fórmula de {e['cell']} empieza por '=' (=SUM(A1:A5)).")
        if "formula" in e and "{" in e["formula"]:  # `_semis` estropearía sus comas
            raise ValueError(f"La fórmula de {e['cell']} lleva una matriz en línea ({{…}}), y no se admiten: usa "
                             "rangos de celdas.")
        if "value" in e and (type(e["value"]) is bool or not isinstance(e["value"], (int, float, str))):
            raise ValueError(f"El valor de {e['cell']} es un número o un texto.")
    p = os.path.expanduser(path)
    with _lock:
        new = not os.path.exists(p)
        src = _out(p, True) if new else safe_file(p)  # inexistente: hoja nueva en esa ruta
        ext = _ext(src, SHEETS)
        target = _out(save_as, approved, src) if save_as else src
        _ext(target, SHEETS)
        if not new and target == src:  # pasar por LibreOffice no es neutro: lo que no entiende, lo pierde
            if ext == ".csv":
                raise LossyInPlace(f"{path}: un CSV no se reescribe en el sitio; LibreOffice lo relee con sus tipos "
                                   "(08001 → 8001, números largos en notación científica).")
            if ext == ".xlsx" and not approved:
                raise NeedsApproval(f"Guardar {path} en el sitio lo reescribe LibreOffice, que puede perder lo que "
                                    "no entiende (macros, gráficos, formatos de Excel).")
        r = _call({"op": "write", "path": src, "new": new, "edits": edits, "sheet": sheet, "target": target})
    return {"sheet": r["sheet"], "lines": r["lines"], "saved_to": target, "created": new}


def convert(path: str, to: str, approved: bool) -> dict:
    """`to`: una extensión (pdf, .xlsx) → junto al original; o una ruta absoluta con la extensión."""
    src = safe_file(path)
    _ext(src, SHEETS | TEXTS)
    target = to if "/" in to else str(Path(src).with_suffix("." + to.lstrip(".").lower()))
    with _lock:
        target = _out(target, approved, src)
        if target == src:
            raise ValueError("Convertir al mismo fichero no hace nada: elige otro formato u otra ruta.")
        _ext(target, (SHEETS if os.path.splitext(src)[1].lower() in SHEETS else TEXTS) | {".pdf"})
        _call({"op": "convert", "path": src, "target": target})
    return {"saved_to": target, "bytes": os.path.getsize(target)}
