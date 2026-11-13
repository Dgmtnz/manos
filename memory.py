"""Memoria por app (PLAN §10.1, skills dinámicas): lo que manos midió de cada app de escritorio, escrito por el
servidor y nunca por un modelo.

- **Cuándo.** Al aceptar `submit_task_complete`, `learn(started)` lee la auditoría de la tarea (las filas desde
  `started`) y, por cada resourceClass con al menos una acción acertada, escribe o pone al día
  `$XDG_DATA_HOME/manos/memory/<clase>.md` (carpeta 0700, fichero 0600).
- **Qué.** Aciertos por peldaño, tool y acción; fallos por tipo de error; sumados a los de tareas anteriores (el
  estado va en la última línea, un comentario JSON). Una vez por app, un sondeo de solo lectura: versión
  (AppStream), número de acciones KXmlGui y elementos AT-SPI con y sin acción. Nunca valores, títulos ni texto:
  de la auditoría solo salen ids y recuentos.
- **Cómo se lee.** server.py la sirve como `skill://memory/<clase>`, la lista en `skill://index` y la nombra en el
  `next` de app_launch y desktop_state.

Autocomprobación (KWin anidada):  uv run --project . tests/test_memory.py
"""

import fcntl
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

import kwin
import verify
from desktop import apps, atspi

TOP = 10  # líneas por lista: el fichero cabe en ~40
_STATE = re.compile(r"<!-- manos-memoria (.*) -->")
_RELEASE = re.compile(r'<release\b[^>]*\bversion="([\w.+~-]{1,32})"')


def _dir() -> Path:
    return verify.data_dir() / "memory"


def path_ok(app) -> bool:
    """Un resourceClass que sirve de nombre de fichero: forma de id, sin '/' y sin punto delante."""
    return isinstance(app, str) and bool(app) and bool(kwin._LEAF.fullmatch(app)) and not app.startswith(".")


def path(app: str) -> Path | None:
    """El fichero de memoria de `app`, si existe y el nombre tiene forma de resourceClass."""
    if not path_ok(app):
        return None
    p = _dir() / f"{app}.md"
    return p if p.is_file() else None


def uri(app: str) -> str:
    return f"skill://memory/{app}"


def hint(classes) -> str | None:
    """El `next` de app_launch y desktop_state: una línea si alguna de esas apps tiene memoria."""
    have = sorted({c for c in classes if c and path(c)})
    if not have:
        return None
    return (f"Hay memoria de {', '.join(have)}: lee {', '.join(f'`{uri(c)}`' for c in have)} antes de actuar "
            "(qué peldaños funcionaron y cuáles fallaron con esa app en tareas anteriores).")


def _state(p: Path | None) -> dict:
    if p:
        try:
            if m := _STATE.search(p.read_text()):
                return json.loads(m[1])
        except (OSError, ValueError):
            pass  # ilegible: se empieza de cero
    return {"tasks": 0, "ok": {}, "fail": {}}


def _rows(started: float) -> list[dict]:
    # ponytail: lee la auditoría entera en cada gate aceptado; si crece mucho, leer desde el final hasta `started`
    try:
        lines = (verify.data_dir() / "audit.jsonl").read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        # solo las de este servidor: una tarea reanudada de otra sesión (mismo nombre, `started` antiguo) o un
        # servidor en paralelo no se cuentan aquí (medido: Dolphin contado dos veces en la prueba real)
        if isinstance(r, dict) and r.get("ts", 0) >= started and r.get("pid") == os.getpid() and r.get("resource_class"):
            rows.append(r)
    return rows


def _version(app: str) -> str | None:
    """La versión de la última entrada <release> de su metainfo de AppStream; sin lanzar nada."""
    dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for d in dirs:
        for suffix in ("metainfo.xml", "appdata.xml"):
            try:
                if m := _RELEASE.search(Path(d, "metainfo", f"{app}.{suffix}").read_text(errors="replace")):
                    return m[1]
            except OSError:
                continue
    return None


def probe(app: str) -> dict | None:
    """Sondeo de solo lectura de una ventana viva de `app`; None si no hay ventana (se reintenta otra vez)."""
    w = next((w for w in kwin.state()["windows"] if w["resource_class"] == app), None)
    if w is None:
        return None
    out = {"version": _version(app)}
    try:
        out["kxmlgui"] = apps.actions(w, "", 1)["total"]
    except Exception as exc:
        out["kxmlgui"] = type(exc).__name__
    try:
        out["atspi"] = atspi.census(w)
    except Exception as exc:
        out["atspi"] = type(exc).__name__
    return out


def _label(r: dict) -> str:
    # una acción que no existe es lo que el modelo inventó, no algo de la app: no se guarda
    act = r.get("action") if r.get("error_type") not in ("NoSuchAction", "NotInstalled") else None
    return f"peldaño {r.get('rung')} · {r['tool']}" + (f" {act}" if act else "")


def _render(app: str, s: dict) -> str:
    p = s.get("probe") or {}
    k, a = p.get("kxmlgui"), p.get("atspi")  # un número o el tipo de error del sondeo
    k = f"{k} acciones (app_actions → app_invoke)" if isinstance(k, int) else k or "sin sondear"
    a = f"{a['nodes']} elementos, {a['actions']} con acción" if isinstance(a, dict) else a or "sin sondear"
    facts = [f"- Versión: {p.get('version') or 'desconocida'}.", f"- Peldaño 1, KXmlGui: {k}.",
             f"- Peldaño 2, AT-SPI: {a}.", f"- Tareas certificadas con esta app: {s['tasks']}."]
    top = lambda d: [f"- {name}: {n}" for name, n in Counter(d).most_common(TOP)] or ["- (nada)"]
    return "\n".join([
        "---", f"name: memory/{app}",
        f"description: {app} — lo que manos midió en {s['tasks']} tareas certificadas: qué peldaños funcionaron y "
        "qué falló.", "---", f"# {app}", "",
        "Escrito por el servidor tras cada tarea certificada: recuentos de la auditoría, sin valores ni títulos. "
        "Un acierto es que la tool contestó ok, no que el resultado fuera el bueno: compruébalo igual.", "",
        *facts, "", "## Funcionó (llamadas ok)", *top(s["ok"]), "", "## Falló (tipo de error)", *top(s["fail"]), "",
        f"<!-- manos-memoria {json.dumps(s, ensure_ascii=False, sort_keys=True)} -->", ""])


def _write(app: str, text: str) -> None:
    d = _dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{app}.", suffix=".tmp")  # 0600
    with open(fd, "w") as f:
        f.write(text)
    os.replace(tmp, d / f"{app}.md")


def learn(started: float, sensitive) -> list[str]:
    """Al aceptar el gate: pone al día la memoria de cada app sobre la que la tarea actuó con éxito. `sensitive` es
    el de server.py: una ventana sensible no se sondea ni se recuerda."""
    by: dict[str, list[dict]] = {}
    for r in _rows(started):
        by.setdefault(r["resource_class"], []).append(r)
    apps_ = [a for a, rows in by.items()
             if path_ok(a) and not sensitive(a) and any(r.get("acting") and r.get("ok") for r in rows)]
    if not apps_:
        return []
    d = _dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # leer-sumar-escribir: dos gates (o dos servidores) a la vez
        return [_learn(app, by[app]) for app in apps_]


def _learn(app: str, rows: list[dict]) -> str:
    """Una app, bajo el cerrojo de `learn`."""
    s = _state(path(app))
    s["tasks"] += 1
    for r in rows:
        key = _label(r) if r.get("ok") else f"{_label(r)} → {r.get('error_type')}"
        bucket = s["ok"] if r.get("ok") else s["fail"]
        bucket[key] = bucket.get(key, 0) + 1
    if not s.get("probe"):
        try:
            s["probe"] = probe(app)
        except Exception:  # KWin no contesta: la próxima tarea lo reintenta
            s["probe"] = None
    _write(app, _render(app, s))
    return app
