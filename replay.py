"""La caché de repetición (PLAN §4.3): los pasos que actúan de una tarea certificada, repetibles sin modelo.

- **Solo trabajo certificado.** Cada tool que actúa apunta aquí su paso acertado (`note`), con un localizador
  duradero y nunca un id efímero: un ref del navegador va como {url del frame, xpath, rol, nombre}; un campo, como
  {k, label, nm, id, section}; un envío, como el texto y la clasificación del botón. Cuando `submit_task_complete`
  se acepta, `take` guarda la tanda en `$XDG_DATA_HOME/manos/replay/<tarea>.json` (carpeta 0700, fichero 0600). Una
  tanda empieza en el task_checkpoint que abre la tarea (`restart`: de antes solo queda la última navegación).
- **Nunca un secreto.** Un campo marcado con un valor literal, una contraseña, una tarjeta, un OTP, una ventana
  sensible, un traspaso, una url con parámetros de credencial, la respuesta escrita de un prompt(), o un valor
  secreto conocido en cualquier paso (lo que `sanitize.scrub` tacharía, salvo los datos del perfil que no
  identifican ni pagan) manchan la tanda (`taint`): no se guarda nada y el gate dice por qué. `{{perfil.clave}}` es
  una referencia, no un dato: se guarda tal cual, y el autofill del perfil como `autofill: true`.
- **Lo que no se puede repetir corta** (`cut`): `ui_act` (sus refs son handles AT-SPI de una sesión, sin camino
  estable), lo que escribe fuera de la página (browser_extract, browser_download, office_write, office_convert) y
  window_set. Se guarda hasta ahí, con el corte como último paso: la repetición para en él y el modelo sigue a mano.
- **Lo que compromete no se repite nunca** (server.py, `_commit`): form_submit, Enter, aceptar un diálogo, un clic
  que navegó o abrió un diálogo, o que parece enviar, pagar o confirmar. La repetición rellena y navega hasta ahí;
  `replays` enseña lo literal que escribiría (`types`) para no usarla con los datos de otra persona.

La repetición la hace `task_replay` en server.py, por las mismas tools (frenos, candado, detector de bucles).

Autocomprobación:  uv run --project . tests/test_replay.py
"""

import json
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path

import refs
import verify
from browser import forms, sanitize

CUTS = {"ui_act", "window_set", "browser_extract", "office_write", "office_convert"}
FIELD_KEYS = ("k", "label", "nm", "id", "section")  # la identidad estable de un campo de formulario
MAX_LISTED = 10
MAX_TYPED, TYPED_LEN = 8, 40  # los valores literales que lista `replays`, y su largo
ID_KEY = re.compile(r"\b(dni|nif|nie|pasaporte|passport)\b", re.I)

_steps: list[dict] = []
_taint = ""


class NoSuchReplay(LookupError):
    """No hay repetición con ese nombre."""


def _sensitive_key(key: str) -> bool:
    """Una clave del perfil que identifica o paga: el nivel B de forms (iban, tarjeta…), el teléfono y los documentos."""
    return forms._looks_secret(key) or bool(forms.PHONE.search(key) or ID_KEY.search(key.replace(".", " ")))


def note(step: dict) -> None:
    """Apunta un paso acertado. Mancha la tanda un valor secreto conocido (lo que `scrub` taparía), una url con
    parámetros de credencial (`clean_url` la recorta) y la respuesta escrita de un prompt(). Los datos del perfil que
    no identifican ni pagan no cuentan: `scrub` los tacha solo para el modelo (medido: la etiqueta del select de
    provincia de form24 lleva el texto «Murcia» de sus opciones, el municipio del perfil)."""
    plain = {d for k, v in forms._profile.items() if not _sensitive_key(k)
             for d in (str(v), *forms._derived(str(v)))}
    if sanitize.scrub(step, set(sanitize._known) - plain) != step:
        taint("un paso lleva un valor secreto")
    if isinstance(step.get("url"), str) and sanitize.clean_url(step["url"]) != step["url"]:
        taint("una url lleva parámetros de credencial")
    if step["tool"] == "browser_dialog" and step.get("text"):
        taint("se contestó un prompt() con texto")
    _steps.append(step)


def restart() -> None:
    """Una tarea nueva: lo hecho antes (una tarea abandonada) no es suyo. Se queda la última navegación, que es
    dónde empieza esta."""
    nav = [s for s in _steps if s["tool"] in ("browser_open", "browser_goto")][-1:]
    _steps[:] = nav


def cut(tool: str) -> None:
    _steps.append({"tool": tool, "cut": True})


def taint(reason: str) -> None:
    global _taint
    _taint = _taint or reason


def act_step(ref_id: int, action: str, value: str, keys: str, files: list[str]) -> dict:
    """El paso de browser_act, tomado ANTES de actuar (un clic que navega retira el ref). Escribir en un ref marcado
    (`sec`) mancha la tanda aunque el servidor lo niegue."""
    r = refs.lookup(ref_id)
    if r.sec and action in ("type", "select", "press"):
        taint("se escribió en un campo marcado como secreto")
    return {"tool": "browser_act", "action": action, "value": value, "keys": keys, "files": [rel(f) for f in files],
            "loc": {"url": sanitize.clean_url(r.url), "xpath": r.xpath, "role": r.role, "name": r.name}}


def fill_step(fields: list[dict], autofill: bool) -> dict:
    """El paso de form_fill, con cada campo por su identidad estable. Un campo de pago, de nivel A, o marcado con un
    valor literal (no `{{perfil.clave}}`) mancha la tanda."""
    out = []
    for item in fields:
        row = forms._get(item.get("id", item.get("i"))).row
        raw = item.get("value", item.get("option", item.get("checked", item.get("file"))))
        if row.get("pay") or row.get("sec") == "A" or \
                row.get("sec") and not (isinstance(raw, str) and forms._REF.fullmatch(raw.strip())):
            taint("un campo secreto (contraseña, tarjeta, OTP o marcado) llevaba un valor escrito")
        out.append({"f": {k: row.get(k, "") for k in FIELD_KEYS}, "value": rel(raw) if row["k"] == "file" else raw})
    return {"tool": "form_fill", "fields": out, "autofill": autofill}


def submit_step(token: str, expect: str, approved: bool) -> dict | None:
    rec = forms._tokens.get(token)
    return rec and {"tool": "form_submit", "button": rec["btn"]["t"], "cls": rec["btn"]["cls"], "expect": expect,
                    "approved": approved}


def desk(tool: str, args: tuple, resource_class: str | None, result: dict) -> None:
    """Las de escritorio que actúan, desde `_desk`: app_launch y app_invoke se apuntan, las de CUTS cortan, y una
    ventana sensible (o del shell) mancha."""
    if not result["ok"]:
        if result["error"]["type"] in ("Refused", "SensitiveWindowOpen"):
            taint("se tocó una ventana sensible")
    elif tool in CUTS:
        cut(tool)
    elif tool == "app_launch":
        note({"tool": tool, "app": args[0], "url": rel(args[1]), "target": args[2]})
    elif tool == "app_invoke":
        note({"tool": tool, "resource_class": resource_class, "action": args[1], "approved": args[2]})


def rel(path):
    """Una ruta dentro del directorio de trabajo se guarda como `./…`: otra vuelta de la tarea la busca en el suyo."""
    cwd = os.getcwd() + os.sep
    return "./" + path.removeprefix(cwd) if isinstance(path, str) and path.startswith(cwd) else path


def unrel(path):
    return os.path.join(os.getcwd(), path[2:]) if isinstance(path, str) and path.startswith("./") else path


def _dir() -> Path:
    return verify.data_dir() / "replay"


def take(task: str, plan: list[str]) -> tuple[bool, str]:
    """Al aceptar el gate: guarda la tanda como `<task>.json` o dice por qué no, y empieza otra."""
    global _taint
    steps, why = list(_steps), _taint
    _steps.clear()
    _taint = ""
    if why:
        return False, f"no se guarda la repetición: {why}."
    end = next((i + 1 for i, s in enumerate(steps) if s.get("cut")), len(steps))
    # una navegación que la siguiente browser_open pisa no hace nada (abrir la url y luego task_replay, que la abre)
    steps = [s for s, nxt in zip(steps[:end], [*steps[1:end], {}])
             if not (s["tool"] in ("browser_open", "browser_goto") and nxt.get("tool") == "browser_open")]
    if not any(not s.get("cut") for s in steps):
        return False, "no hubo pasos que repetir."
    h = verify._sha(steps)
    d = _dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    for p in d.glob("*.json"):
        try:
            if json.loads(p.read_text()).get("hash") == h:
                return True, f"ya estaba guardada como «{p.stem}»."
        except (OSError, ValueError):
            continue
    first = steps[0]
    start = first.get("url") or first.get("app") or first.get("resource_class") or ""
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{task}.", suffix=".tmp")  # 0600
    with open(fd, "w") as f:
        json.dump({"name": task, "saved": time.time(), "plan": plan, "start": start, "hash": h, "steps": steps}, f,
                  ensure_ascii=False, indent=1)
    os.replace(tmp, d / f"{task}.json")
    return True, f"guardada como «{task}» ({len(steps)} pasos): task_replay la repite."


def listing() -> list[dict]:
    """Las repeticiones guardadas, la más reciente primero: nombre, plan, dónde empieza y qué pasos lleva."""
    try:
        paths = sorted(_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out = []
    for p in paths[:MAX_LISTED]:
        try:
            r = json.loads(p.read_text())
            tools = Counter(s["tool"] for s in r["steps"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append({"name": p.stem, "plan": "; ".join(r.get("plan", []))[:300], "start": r.get("start", ""),
                    "steps": ", ".join(f"{t}×{n}" if n > 1 else t for t, n in tools.items()),
                    "types": typed(r["steps"])})
    return out


def typed(steps: list[dict]) -> list[str]:
    """Lo literal que la repetición escribiría (no `{{perfil.clave}}`): para ver «Juan…» antes de usarla para Pedro."""
    vals = [f["value"] for s in steps if s["tool"] == "form_fill" for f in s["fields"]] + \
           [s["value"] for s in steps if s["tool"] == "browser_act" and s["action"] in ("type", "select")]
    lit = [v[:TYPED_LEN] for v in vals if isinstance(v, str) and forms._REF.sub("", v).strip()]
    return sanitize.scrub(list(dict.fromkeys(lit))[:MAX_TYPED])


def load(name: str) -> list[dict]:
    if not isinstance(name, str) or not verify.SLUG.fullmatch(name):
        raise ValueError("`name` es el de una repetición de `replays` (lo lista task_checkpoint).")
    try:
        steps = json.loads((_dir() / f"{name}.json").read_text())["steps"]
    except FileNotFoundError:
        raise NoSuchReplay(f"No hay ninguna repetición «{name}».") from None
    except (ValueError, KeyError, TypeError):
        raise NoSuchReplay(f"La repetición «{name}» está rota.") from None
    if not isinstance(steps, list) or not all(isinstance(s, dict) and isinstance(s.get("tool"), str) for s in steps):
        raise NoSuchReplay(f"La repetición «{name}» está rota.")
    return steps


def find_ref(loc: dict) -> tuple[int | None, str]:
    """El ref de la última observación con la misma url de frame y el mismo xpath, y el mismo rol y nombre; si no,
    (None, por qué). Igual que browser_act: nunca otro elemento que casualmente esté en el sitio."""
    same = [(i, r) for i, r in refs._refs.items()
            if r.xpath == loc["xpath"] and sanitize.clean_url(r.url) == loc["url"]]
    for i, r in same:
        if (r.role, r.name) == (loc["role"], loc["name"]):
            return i, ""
    if same:
        r = same[0][1]
        return None, f"cambió (era {loc['role']} «{loc['name']}», ahora {r.role} «{r.name}»)"
    return None, f"no está {loc['role']} «{loc['name']}»"


def find_field(desc: dict) -> tuple[int | None, str]:
    """El id de la última form_describe con la misma identidad estable; ninguno o dos = (None, por qué)."""
    hit = [fid for fid, f in forms._fields.items() if all(f.row.get(k, "") == desc[k] for k in FIELD_KEYS)]
    if len(hit) == 1:
        return hit[0], ""
    return None, f"{'no hay' if not hit else 'hay más de un'} campo «{desc['label']}» ({desc['k']})"
