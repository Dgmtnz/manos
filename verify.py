"""Verificación y parada de manos (PLAN §8.1–8.2): ids de observación, tareas, el finish gate y el detector de bucles.

- **Ids de observación.** Cada tool que observa acuña uno al azar (`obs-<12 hex>`): el modelo no puede inventar uno
  que no se le dio, así que la evidencia de `submit_task_complete` prueba procedencia y no prosa.
- **Tareas (E3).** Un JSON por tarea en `$XDG_DATA_HOME/manos/tasks/<tarea>.json`; el primer checkpoint fija el
  inicio y los criterios de aceptación (`accept`), que luego solo crecen o se corrigen con `revise` y un motivo,
  apuntado en `revisions`.
- **Finish gate.** `accept` más las aserciones de la evidencia se vuelven a correr AHORA en el servidor,
  deterministas. De un fichero salen existencia, tamaño y booleanos de coincidencia; nunca su contenido ni su hash.
- **Bucles (E2).** Huella de `(tool, args)` y hash del estado tras la acción: la tercera acción idéntica seguida con
  el estado sin cambiar se rechaza (`Stuck`) sin hacerse. Más el contador de fallos seguidos por `(tool, diana)`.

Autocomprobación:  uv run --project . tests/test_verify.py
"""

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path

from browser.session import forbidden


class Stuck(RuntimeError):
    """La misma acción por tercera vez seguida sin que cambie nada; el mensaje es para el modelo."""


class NoSuchTask(LookupError):
    """La tarea no tiene checkpoint."""


class BadEvidence(ValueError):
    """La evidencia no se puede comprobar tal como viene; el mensaje dice qué falta."""


class NotDone(RuntimeError):
    """Alguna aserción falló al volver a correrla."""


def data_dir() -> Path:
    """`$XDG_DATA_HOME/manos`, leído en cada llamada: en la KWin anidada es el directorio privado de la anidada."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "manos"


# --- ids de observación -------------------------------------------------------------------------------

_obs: dict[str, dict] = {}
MAX_OBS = 4096
MAX_AGE = 30 * 60  # s: una observación más vieja no prueba nada del estado de ahora


def mint(kind: str, window: str = "", url: str = "") -> str:
    """Acuña el id de una observación y recuerda qué tool fue, cuándo, y su ventana o su url (la cruda: no sale)."""
    oid = "obs-" + secrets.token_hex(6)
    _obs[oid] = {"kind": kind, "t": time.time(), "window": window or "", "url": url or ""}
    if len(_obs) > MAX_OBS:
        del _obs[next(iter(_obs))]  # el más viejo: los dicts guardan el orden de inserción
    return oid


# --- tareas -------------------------------------------------------------------------------------------

SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_ITEM, MAX_ITEMS, MAX_NOTE, MIN_REASON = 200, 50, 1000, 20


def _path(task: str) -> Path:
    if not isinstance(task, str) or not SLUG.fullmatch(task):
        raise ValueError(f"`task` es un identificador corto: minúsculas, cifras, '-' y '_', hasta 64 "
                         f"(p. ej. 'informe-ventas'). No vale {task!r:.80}.")
    return data_dir() / "tasks" / f"{task}.json"


@contextlib.contextmanager
def _locked():
    """Un cerrojo para todo el leer-cambiar-escribir de las tareas: dos servidores (o dos llamadas) a la vez."""
    d = data_dir() / "tasks"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def resume(task: str) -> dict:
    path = _path(task)
    try:
        t = json.loads(path.read_text())
        if not isinstance(t, dict) or not isinstance(t.get("started"), (int, float)):
            raise ValueError("sin forma de tarea")
    except FileNotFoundError:
        raise NoSuchTask(f"No hay ninguna tarea '{task}'.") from None
    except (ValueError, RecursionError):
        os.replace(path, path.with_suffix(".corrupt"))  # se guarda para mirarlo; un checkpoint la crea de nuevo
        raise NoSuchTask(f"La tarea '{task}' estaba corrupta (apartada como {path.stem}.corrupt): ábrela otra vez "
                         "con task_checkpoint.") from None
    return {"accept": [], **t}


def _save(t: dict) -> dict:
    path = _path(t["task"])
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{t['task']}.", suffix=".tmp")  # 0600 y nombre único
    with open(fd, "w") as f:
        f.write(json.dumps(t, ensure_ascii=False, indent=1))
    os.replace(tmp, path)  # un checkpoint a medias nunca deja el JSON roto
    return t


def _items(name: str, items) -> list[str]:
    if not isinstance(items, list) or len(items) > MAX_ITEMS or \
            not all(isinstance(s, str) and 0 < len(s) <= MAX_ITEM for s in items):
        raise ValueError(f"`{name}` es una lista de hasta {MAX_ITEMS} pasos cortos (1-{MAX_ITEM} caracteres).")
    return items


def _revise(revise) -> list[dict]:
    """`revise` = [{"i": <índice en accept>, "assertion": <aserción> | null, "reason": <por qué estaba mal>}]."""
    if not isinstance(revise, list) or len(revise) > MAX_ASSERTIONS:
        raise ValueError(f"`revise` es una lista de hasta {MAX_ASSERTIONS} correcciones.")
    for r in revise:
        if not (isinstance(r, dict) and type(r.get("i")) is int and "assertion" in r):
            raise BadEvidence('Cada corrección es {"i": <índice del criterio>, "assertion": <la nueva> o null para '
                              'quitarlo, "reason": <por qué estaba mal>}.')
        if not (isinstance(r.get("reason"), str) and MIN_REASON <= len(r["reason"].strip()) <= MAX_NOTE):
            raise BadEvidence(f"Corregir el criterio {r['i']} pide `reason`: por qué estaba mal ({MIN_REASON}-"
                              f"{MAX_NOTE} caracteres). Nunca para que pase un trabajo sin hacer o mal hecho.")
        if r["assertion"] is not None:
            _assertion(r["assertion"], r["i"], None)
    if len({r["i"] for r in revise}) < len(revise):
        raise BadEvidence("`revise` corrige cada criterio una vez por llamada.")
    return revise


def checkpoint(task: str, plan: list[str], done: list[str], note: str, accept: list | None = None,
               revise: list | None = None) -> dict:
    """Crea la tarea (el primero fija `started`) o la pone al día: `plan` no vacío la sustituye, `done` se suma,
    `note` se añade y `accept` solo puede crecer. `revise` corrige o quita criterios con un motivo (los índices son
    los de antes de la llamada; se aplica antes que `accept`) y queda en `revisions`. Una tarea ya terminada vuelve a
    empezar: inicio nuevo, nada hecho."""
    plan, done, accept, revise = _items("plan", plan), _items("done", done), accept or [], _revise(revise or [])
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ValueError(f"`note` es texto de hasta {MAX_NOTE} caracteres.")
    if not isinstance(accept, list) or len(accept) > MAX_ASSERTIONS:
        raise ValueError(f"`accept` es una lista de hasta {MAX_ASSERTIONS} aserciones.")
    for i, a in enumerate(accept):
        _assertion(a, i, None)  # BadEvidence antes de tocar nada
    with _locked():
        try:
            t = resume(task)
        except NoSuchTask:
            t = {"task": task, "started": time.time(), "plan": [], "done": [], "notes": [], "status": "open",
                 "accept": []}
        if t["status"] == "done":  # el mismo nombre otra vez es otra vuelta: lo de antes no cuenta como hecho
            t |= {"started": time.time(), "done": [], "status": "open"}
            t.pop("completed", None)
            t.pop("evidence", None)
        if revise:
            if not all(0 <= r["i"] < len(t["accept"]) for r in revise):
                raise BadEvidence(f"`revise`: la tarea tiene {len(t['accept'])} criterios (índices desde 0).")
            new, now = list(t["accept"]), time.time()
            for r in revise:
                new[r["i"]] = r["assertion"]
            if all(a is None for a in new):
                raise BadEvidence("`revise` dejaría la tarea sin criterios: corrige el último en vez de quitarlo.")
            t["revisions"] = [*t.get("revisions", []), *({"ts": now, "i": r["i"], "old": t["accept"][r["i"]],
                                                          "new": r["assertion"], "reason": r["reason"]} for r in revise)]
            t["accept"] = [a for a in new if a is not None]
        if accept:
            if accept[:len(t["accept"])] != t["accept"]:
                raise BadEvidence("`accept` solo puede crecer: pasa los criterios que ya tiene, en el mismo orden, y "
                                  "añade detrás. Uno mal puesto se corrige con `revise` y el motivo.")
            t["accept"] = accept
        if plan:
            t["plan"] = plan
        t["done"] += [d for d in done if d not in t["done"]]
        if note:
            t["notes"] = [*t["notes"], {"t": time.time(), "note": note}][-MAX_ITEMS:]
        return _save(t)


# --- el finish gate -----------------------------------------------------------------------------------

CHECKS = {
    "file": {"exists", "min_size", "sha256", "regex", "json", "modified_after_start"},
    "process": {"systemd_unit_active"},
    "browser": {"selector_present", "selector_absent", "text_regex", "url_regex"},
    "window": {"exists", "title_regex"},
}
WINDOWED = {"ui_snapshot", "screen_capture", "browser_observe", "form_describe"}  # desktop_state no: es todo el escritorio
UNIT = re.compile(r"[A-Za-z0-9:_.@\\-]{1,200}\.(service|socket|timer|target|path|scope|mount)")
MAX_ASSERTIONS, MAX_PATTERN = 32, 500
MAX_READ, MAX_PAGE = 4 << 20, 200_000
# ponytail: regex y JSON leen los primeros 4 MiB de un fichero y 200 KB del texto de la página; `re` no tiene plazo,
# así que un patrón catastrófico puede colgar un hilo. Si hace falta más, un trabajador con plazo.
NEXT = {  # qué hacer cuando falla cada comprobación
    "file.exists": "Créalo (o guárdalo) donde dice `path` y vuelve a enviar. Sirve para ficheros y carpetas.",
    "file.min_size": "El fichero es más pequeño de lo esperado: termina de escribirlo y guárdalo.",
    "file.sha256": "El contenido no es el esperado: revísalo y guárdalo otra vez.",
    "file.regex": "El contenido no tiene lo que pide el patrón: corrígelo y guárdalo.",
    "file.json": "No es JSON válido: corrígelo en la posición que dice `observed`.",
    "file.modified_after_start": "No se ha tocado desde que empezó la tarea: guarda el trabajo de esta tarea.",
    "process.systemd_unit_active": "La unidad no está activa: arráncala o mira por qué falló.",
    "browser.selector_present": "No está en la página: browser_observe para ver dónde estás.",
    "browser.selector_absent": "Sigue en la página: browser_observe para ver qué falta.",
    "browser.text_regex": "La página no dice eso: browser_observe para ver qué dice.",
    "browser.url_regex": "El navegador está en otra url: browser_observe o browser_open.",
    "window.exists": "No hay esa ventana: desktop_state, o app_launch si hay que abrirla.",
    "window.title_regex": "El título no casa: desktop_state para ver el título y la ventana.",
}


def _vedada(path: str) -> bool:
    return forbidden(path)  # la regla de rutas de manos, una sola: ver browser.session.forbidden


def _regex(a: dict, where: str) -> re.Pattern:
    v = a.get("value")
    if not isinstance(v, str) or not v or len(v) > MAX_PATTERN:
        raise BadEvidence(f"{where} pide `value`: una regex de hasta {MAX_PATTERN} caracteres.")
    try:
        rx = re.compile(v)
    except re.error as e:
        raise BadEvidence(f"{where}: la regex no compila ({e}).") from None
    if rx.search(""):
        raise BadEvidence(f"{where}: la regex casa con el texto vacío, así que no puede fallar. Pide algo concreto.")
    return rx


def _assertion(a, i: int, obs: dict | None) -> dict:
    """Una aserción comprobable o BadEvidence. Con `obs` None (al fijar `accept`) solo se mira la forma."""
    kind, check = (a.get("kind"), a.get("check")) if isinstance(a, dict) else (None, None)
    if not (isinstance(kind, str) and isinstance(check, str) and check in CHECKS.get(kind, ())):
        raise BadEvidence(f"La aserción {i} no es de un tipo que el servidor sepa comprobar. Hay: "
                          + "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in CHECKS.items())
                          + ". Red, código de salida y hash perceptual no existen.")
    where = f"La aserción {i} ({kind}.{check})"
    a = {**a, "i": i}
    if kind == "file":
        if not isinstance(a.get("path"), str) or not os.path.isabs(a["path"]):
            raise BadEvidence(f"{where} pide `path` absoluto.")
        if _vedada(a["path"]):
            raise BadEvidence(f"{where}: manos no lee carpetas ocultas de la casa (~/.ssh, ~/.config…), /proc, "
                                  "/sys, /dev ni el perfil del navegador.")
        if check == "min_size" and not (type(a.get("value")) is int and a["value"] >= 1):
            raise BadEvidence(f"{where} pide `value`: bytes, entero ≥ 1.")
        if check == "sha256" and not (isinstance(a.get("value"), str) and re.fullmatch(r"[0-9a-fA-F]{64}", a["value"])):
            raise BadEvidence(f"{where} pide `value`: 64 cifras hexadecimales.")
    elif kind == "process" and not (isinstance(a.get("unit"), str) and UNIT.fullmatch(a["unit"])):
        raise BadEvidence(f"{where} pide `unit`: el nombre de una unidad de systemd --user (foo.service).")
    elif kind == "browser":
        if check.startswith("selector") and not (isinstance(a.get("value"), str) and a["value"]):
            raise BadEvidence(f"{where} pide `value`: un selector CSS.")
        if obs is not None and obs["kind"] not in ("browser_observe", "form_describe"):
            raise BadEvidence(f"{where} necesita una observación de la página (browser_observe o form_describe): "
                              "observa de nuevo y usa ese id.")
    elif kind == "window":
        for k in ("uuid", "resource_class"):
            if not isinstance(a.get(k, ""), str):
                raise BadEvidence(f"{where}: `{k}` es texto.")
        if obs is not None:
            a.setdefault("uuid", "" if a.get("resource_class") else obs["window"] if obs["kind"] in WINDOWED else "")
            if not (a["uuid"] or a.get("resource_class")):
                raise BadEvidence(f"{where} pide `uuid` o `resource_class`, o una observación de una ventana "
                                  "(ui_snapshot o screen_capture de esa ventana).")
    if check.endswith("regex"):
        a["re"] = _regex(a, where)
    return a


def validate(task: str, evidence) -> tuple[dict, dict, list[dict]]:
    """(tarea, observación, aserciones de `accept` + evidencia) o BadEvidence/NoSuchTask. Nada se comprueba si algo
    no se puede comprobar."""
    t = resume(task)
    if t["status"] == "done":
        raise BadEvidence(f"La tarea '{task}' ya está terminada. Para repetirla, task_checkpoint la reabre con una "
                          "hora de inicio nueva.")
    if not t["accept"]:
        raise BadEvidence("La tarea no tiene criterios de aceptación: fija los criterios con "
                          "task_checkpoint(accept=…) antes de empezar.")
    extra = evidence.get("assertions", []) if isinstance(evidence, dict) else None
    if not isinstance(extra, list) or len(extra) > MAX_ASSERTIONS:
        raise BadEvidence("`evidence` es {\"observation_id\": …, \"assertions\": [ … ]}, con hasta "
                          f"{MAX_ASSERTIONS} aserciones además de las de `accept`.")
    obs = _obs.get(evidence.get("observation_id")) if isinstance(evidence.get("observation_id"), str) else None
    if obs is None:
        raise BadEvidence("`observation_id` no es de ninguna observación que este servidor recuerde (puede que se "
                          "haya olvidado o que el servidor se reiniciara): observa otra vez y usa el id nuevo.")
    if time.time() - obs["t"] > MAX_AGE:
        raise BadEvidence(f"La observación tiene más de {MAX_AGE // 60} min: vuelve a observar y usa el id nuevo.")
    if obs["t"] < t["started"]:
        raise BadEvidence("La observación es de antes de empezar la tarea: vuelve a observar y usa el id nuevo.")
    return t, obs, [_assertion(a, i, obs) for i, a in enumerate([*t["accept"], *extra])]


def _file(a: dict, started: float) -> tuple[bool, object]:
    check, path = a["check"], Path(a["path"])
    if _vedada(a["path"]):  # otra vez al leer: un enlace pudo cambiar desde la validación
        return False, "ruta vedada"
    try:
        st = path.stat()
    except OSError:
        return False, "no existe"
    is_dir = stat.S_ISDIR(st.st_mode)
    if not (stat.S_ISREG(st.st_mode) or is_dir and check in ("exists", "modified_after_start")):
        return False, "es una carpeta: con ella solo valen exists y modified_after_start" if is_dir else \
            "no es un fichero regular"
    if check == "exists":
        return True, "carpeta" if is_dir else f"{st.st_size} bytes"
    if check == "min_size":
        return st.st_size >= a["value"], f"{st.st_size} bytes"
    if check == "modified_after_start":
        return st.st_mtime > started, f"modificado {st.st_mtime - started:+.1f} s respecto al inicio de la tarea"
    if check == "sha256":
        with path.open("rb") as f:
            ok = hashlib.file_digest(f, "sha256").hexdigest() == a["value"].lower()
        return ok, "igual" if ok else "distinto"  # el hash de verdad no sale: es una huella del contenido
    with path.open("rb") as f:
        data = f.read(MAX_READ)
    if check == "regex":
        hit = bool(a["re"].search(data.decode("utf-8", "replace")))
        return hit, f"coincide: {hit}"
    try:  # json
        json.loads(data)
        return True, "JSON válido"
    except RecursionError:
        return False, "no es JSON válido (anidado demasiado hondo)"
    except ValueError as e:
        return False, f"no es JSON válido (línea {getattr(e, 'lineno', '?')}, columna {getattr(e, 'colno', '?')})"


def _unit(a: dict) -> tuple[bool, object]:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "--", a["unit"]],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"systemctl no contestó ({type(e).__name__})"
    return r.returncode == 0, r.stdout.strip() or "sin respuesta"


def by_class(windows: list[dict], name: str) -> list[dict]:
    """Las ventanas de un resourceClass: el exacto o, si no hay, el nombre corto (`dolphin` → `org.kde.dolphin`).
    La usan el gate y `_window` de server.py: las dos dianas se resuelven igual."""
    return [w for w in windows if w["resource_class"] == name] or \
           [w for w in windows if name and (w["resource_class"] or "").rsplit(".", 1)[-1].lower() == name.lower()]


def _window(a: dict, windows: list[dict]) -> tuple[bool, object]:
    hit = [w for w in windows if w["uuid"] == a["uuid"]] if a["uuid"] else by_class(windows, a.get("resource_class"))
    if a["check"] == "exists":
        return bool(hit), f"{len(hit)} ventana(s)"
    ok = any(a["re"].search(w["caption"] or "") for w in hit)
    return ok, f"coincide: {ok}" if hit else "no hay esa ventana"


async def _browser(a: dict, page) -> tuple[bool, object]:
    check = a["check"]
    if check == "url_regex":
        ok = bool(a["re"].search(page.url))
        return ok, f"coincide: {ok}"
    if check == "text_regex":
        text = await page.evaluate(f"() => (document.body ? document.body.innerText : '').slice(0, {MAX_PAGE})")
        ok = bool(await asyncio.to_thread(a["re"].search, text))
        return ok, f"coincide: {ok}"
    try:
        n = await page.evaluate("s => document.querySelectorAll(s).length", a["value"])
    except Exception as e:
        return False, f"el selector no vale ({type(e).__name__})"
    return (n > 0) == (check == "selector_present"), f"{n} elemento(s)"


async def run(t: dict, obs: dict, asserts: list[dict], page=None, windows: list[dict] | None = None) -> list[dict]:
    """Corre cada aserción ahora. `page` y `windows` los da el servidor, solo si hay aserciones que los piden."""
    browser = [a for a in asserts if a["kind"] == "browser"]
    if browser and page.url != obs["url"] and not any(a["check"] == "url_regex" for a in browser):
        raise BadEvidence("La página ya no es la que observaste: observa de nuevo con browser_observe y usa ese id "
                          "(o añade una aserción url_regex).")
    out = []
    for a in asserts:
        kind = a["kind"]
        if kind == "file":
            ok, seen = await asyncio.to_thread(_file, a, t["started"])
        elif kind == "process":
            ok, seen = await asyncio.to_thread(_unit, a)
        elif kind == "window":
            ok, seen = _window(a, windows)
        else:
            ok, seen = await _browser(a, page)
        target = a.get("path") or a.get("unit") or a.get("uuid") or a.get("resource_class") or a.get("value") or ""
        out.append({"i": a["i"], "kind": kind, "check": a["check"], "target": str(target)[:200], "ok": ok,
                    "observed": seen, "next": "" if ok else NEXT[f"{kind}.{a['check']}"]})
    return out


def finish(task: str, started: float, results: list[dict], observation_id: str) -> dict:
    """Si todo pasó, marca la tarea hecha, releyéndola bajo el cerrojo. Devuelve lo que ve el modelo."""
    failed = [r for r in results if not r["ok"]]
    with _locked():
        t = resume(task)
        if t["status"] == "done" or t["started"] != started:
            raise BadEvidence(f"La tarea '{task}' cambió mientras se comprobaba (otra llamada la cerró o la reabrió): "
                              "task_resume y vuelve a enviar.")
        if not failed:
            t |= {"status": "done", "completed": time.time(),
                  "evidence": {"observation_id": observation_id, "assertions": len(results)}}
            _save(t)
    return {"status": t["status"], "passed": len(results) - len(failed), "failed": failed,
            "revisions": len(t.get("revisions", []))}


# --- detector de bucles -------------------------------------------------------------------------------

# lo que cambia en cada llamada sin decir nada del estado: tiempos, tokens de un solo uso, el estado de ANTES
VOLATILE = {"before", "ms", "ms_per_field", "submit_tokens", "screenshot", "hint", "observation_id"}
SWITCH = {  # el peldaño siguiente por tool, para Stuck y para la pista de fallos
    "ui_act": "cambia de mecanismo: app_actions/app_invoke (peldaño 1) si es una app KDE, o screen_capture para "
              "ver qué está pasando.",
    "app_invoke": "cambia de mecanismo: ui_snapshot + ui_act (peldaño 2) sobre la misma ventana.",
    "window_set": "relee la ventana con desktop_state: KWin puede estar imponiendo esa geometría.",
    "app_launch": "mira desktop_state: puede que la app ya esté abierta.",
    "form_fill": "vuelve a form_describe (los ids pueden haber cambiado) o mira la página con browser_observe.",
    "form_submit": "vuelve a form_fill para un submit_token nuevo y mira `blocking`.",
    "browser_dialog": "mira la página con browser_observe: puede que no haya ningún diálogo.",
    "browser_act": "mira la página con browser_observe (algo puede taparlo o no reaccionar a eso) y prueba otro ref "
                   "u otro verbo; un formulario va por form_describe/form_fill.",
    "browser_extract": "mira la página con browser_observe(mode=\"text\"): el selector, el botón o la url pueden no "
                       "ser los buenos.",
    "browser_page_tool": "mira la página con browser_observe: el `input_schema` y el nombre están en `page_tools`; si "
                         "la tool no sirve, browser_act.",
    "browser_download": "mira la página con browser_observe: la url puede no ser la del fichero (el `src` de la "
                        "imagen, el `href` del enlace) o pedir sesión; si no, browser_handoff.",
}
FAIL_TTL = 5 * 60  # s: un fallo de hace más no cuenta como seguido
_tail: deque = deque(maxlen=2)  # [huella, hash del estado, ok] de las dos últimas acciones; browser_open lo vacía
_fails: dict[tuple[str, str], tuple[int, float]] = {}
# ponytail: solo dos acciones SEGUIDAS; un bucle que alterna dos acciones muertas (A, B, A, B) no se ve. Si el
# banco lo pide, una ventana más larga por huella, con cuidado de no rechazar tras un cambio hecho por otra tool.


def _sha(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def fingerprint(tool: str, args) -> str:
    """Huella de `(tool, args canonicalizados)`. Solo se guarda el hash: los valores (un texto, una contraseña de
    un prompt) nunca se quedan en memoria."""
    return _sha([tool, args])


def repeated(fp: str) -> bool:
    """¿Es la misma acción que la última?"""
    return bool(_tail) and _tail[-1][0] == fp


def guard(tool: str, fp: str, before=...) -> None:
    """Lanza Stuck si es la tercera acción idéntica seguida y la segunda no cambió nada.

    `before` es el hash del estado justo ANTES de esta acción, para las tools cuyo resultado no lo dice (ui_act):
    sin acciones en medio, es el estado de DESPUÉS de la anterior, y así se le apunta. None = ilegible: no rechaza.
    app_invoke solo es Stuck tras dos errores: una acción que no conmuta nada (zoom, siguiente) repite resultado
    aunque haga su trabajo."""
    if before is not ... and repeated(fp):
        _tail[-1][1] = before
    if len(_tail) == 2 and _tail[0][0] == _tail[1][0] == fp and _tail[0][1] is not None and _tail[0][1] == _tail[1][1] \
            and not (tool == "app_invoke" and (_tail[0][2] or _tail[1][2])):
        raise Stuck(f"Es la tercera {tool} idéntica seguida y la anterior no cambió nada: no se repite. "
                    + SWITCH[tool][0].upper() + SWITCH[tool][1:])


def record(tool: str, fp: str, target, result: dict) -> str | None:
    """Apunta la acción y su estado (el propio resultado, sin lo volátil, más `state` si la tool lo da) y lleva la
    cuenta de fallos seguidos por `(tool, diana)`. Devuelve la pista del 2º fallo seguido, o None."""
    state = result.pop("state", None)
    err = (result.get("error") or {}).get("type")
    if err in ("Busy", "Cancelled", "Stuck", "UserDriving"):  # no se hizo nada: ni cuenta en la ventana ni como fallo
        return None
    ok = bool(result.get("ok"))
    _tail.append([fp, _sha([{k: v for k, v in result.items() if k not in VOLATILE}, state]), ok])
    if ok:
        _fails.clear()  # cualquier acierto: lo que viene después ya no es «seguido»
        return None
    key, now = (tool, str(target)), time.time()
    n, t = _fails.get(key, (0, now))
    n = n + 1 if now - t <= FAIL_TTL else 1
    _fails[key] = (n, now)
    return f"{n} fallos seguidos de {tool}{f' en {target}' if target != '' else ''}: {SWITCH[tool]}" if n >= 2 else None
