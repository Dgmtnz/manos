"""Autocomprobación de verify.py (puro, sin escritorio ni navegador):  uv run --project . tests/test_verify.py

Tareas en un XDG_DATA_HOME temporal (slug, inicio, plan/hecho/notas, `accept` que solo crece, reabrir una terminada,
JSON corrupto, modos 0600); corregir criterios con `revise` (motivo obligatorio, apuntado, sin él en la auditoría); el finish gate sobre ficheros temporales (cada comprobación, en verde y en rojo, sin que
salga el contenido ni el hash), página y ventanas falsas; lo que se rechaza sin comprobar nada (aserciones que no
pueden fallar, rutas vedadas, observaciones que no encajan o caducadas); y el detector de bucles con resultados falsos.
"""

import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server
import verify
from browser.session import PROFILE


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return str(e) or type(e).__name__
    return ""


tmp = Path(tempfile.mkdtemp(prefix="manos-verify-"))
os.environ["XDG_DATA_HOME"] = str(tmp / "data")
TASKS = tmp / "data/manos/tasks"
SECRET = "contraseña-que-no-sale-1234"
f = tmp / "informe.txt"
F = lambda check, path=f, **kw: {"kind": "file", "check": check, "path": str(path), **kw}
ACCEPT = [F("exists")]

# --- tareas ---
for bad in ("", "../x", "a/b", "Mayus", ".oculta", "a" * 65, "con espacio", None):
    assert raises(ValueError, verify.checkpoint, bad, [], [], ""), bad
assert raises(ValueError, verify.checkpoint, "t", ["x" * 201], [], "")
assert raises(ValueError, verify.checkpoint, "t", [""], [], "")
assert raises(verify.BadEvidence, verify.checkpoint, "t", [], [], "", [{"kind": "file", "check": "exists"}])
assert not (TASKS / "t.json").exists()  # una `accept` mala no crea la tarea
assert raises(verify.NoSuchTask, verify.resume, "nada")
t0 = time.time()
t = verify.checkpoint("informe-1", ["abrir", "guardar", "comprobar"], [], "empiezo", ACCEPT)
assert t["status"] == "open" and t0 <= t["started"] <= time.time() and t["notes"][0]["note"] == "empiezo", t
assert t["accept"] == ACCEPT
time.sleep(0.01)
t = verify.checkpoint("informe-1", [], ["abrir"], "")
t = verify.checkpoint("informe-1", [], ["abrir", "guardar"], "otra")
r = verify.resume("informe-1")
assert r == t and r["plan"] == ["abrir", "guardar", "comprobar"] and r["done"] == ["abrir", "guardar"], r
assert [n["note"] for n in r["notes"]] == ["empiezo", "otra"] and r["started"] == t["started"] < r["notes"][1]["t"], r
assert not list(TASKS.glob("*.tmp")) and not list(TASKS.glob(".*.tmp"))
assert stat.S_IMODE((TASKS / "informe-1.json").stat().st_mode) == 0o600 and stat.S_IMODE(TASKS.stat().st_mode) == 0o700
# `accept` solo crece: ni cambiar, ni quitar, ni reordenar
assert "solo puede crecer" in raises(verify.BadEvidence, verify.checkpoint, "informe-1", [], [], "", [F("min_size", value=1)])
assert raises(verify.BadEvidence, verify.checkpoint, "informe-1", [], [], "", [F("min_size", value=1), *ACCEPT])
assert verify.checkpoint("informe-1", [], [], "", [])["accept"] == ACCEPT  # vacía: no toca nada
# JSON corrupto: se aparta y un checkpoint la crea de nuevo
(TASKS / "rota.json").write_text("{no es json")
assert "corrupta" in raises(verify.NoSuchTask, verify.resume, "rota") and (TASKS / "rota.corrupt").exists()
assert verify.checkpoint("rota", ["a"], [], "")["status"] == "open"
(TASKS / "rota.json").write_text("[1, 2]")
assert raises(verify.NoSuchTask, verify.resume, "rota")

# --- finish gate: ficheros ---
time.sleep(0.01)
f.write_text(f"Total: 42\n{SECRET}\n")  # escrito después de empezar la tarea
j = tmp / "datos.json"
j.write_text('{"a": 1,}')
deep = tmp / "hondo.json"
deep.write_text("[" * 100_000 + "]" * 100_000)
old = tmp / "viejo.txt"
old.write_text("x")
os.utime(old, (t["started"] - 60, t["started"] - 60))
sha = hashlib.sha256(f.read_bytes()).hexdigest()
obs = verify.mint("ui_snapshot", "uuid-kcalc")


def gate(asserts, page=None, windows=None, task="informe-1", oid=None):
    """validate + run, sin el criterio de `accept` (que va el primero)."""
    tk, ob, a = verify.validate(task, {"observation_id": oid or obs, "assertions": asserts})
    return asyncio.run(verify.run(tk, ob, a, page, windows))[len(tk["accept"]):]


res = gate([F("exists"), F("min_size", value=10), F("sha256", value=sha.upper()), F("regex", value=r"Total: \d+"),
            F("json", j), F("modified_after_start"), F("modified_after_start", old), F("exists", tmp / "no.txt"),
            F("min_size", value=10 ** 6), F("regex", value="Total: 43"), F("json", tmp), F("json", deep),
            F("sha256", value="0" * 64)])
assert [r["ok"] for r in res] == [True, True, True, True, False, True, False, False, False, False, False, False,
                                  False], res
assert res[4]["observed"].startswith("no es JSON válido (línea 1") and res[4]["next"], res[4]
assert res[7]["observed"] == "no existe" and "Créalo" in res[7]["next"], res[7]
assert res[2]["observed"] == "igual" and res[12]["observed"] == "distinto" and res[9]["observed"] == "coincide: False"
assert "hondo" in res[11]["observed"] or "no es JSON" in res[11]["observed"], res[11]
assert SECRET not in json.dumps(res, ensure_ascii=False) and sha not in json.dumps(res) and \
    res[3]["observed"] == "coincide: True", res
# una carpeta: exists y modified_after_start sí; lo que lee contenido, no
carpeta = tmp / "Facturas 2026"
carpeta.mkdir()
res = gate([F("exists", carpeta), F("modified_after_start", carpeta), F("min_size", carpeta, value=1),
            F("regex", carpeta, value="x"), F("modified_after_start", tmp / "no-existe")])
assert [r["ok"] for r in res] == [True, True, False, False, False], res
assert res[0]["observed"] == "carpeta" and "es una carpeta" in res[2]["observed"], res
os.utime(carpeta, (t["started"] - 60, t["started"] - 60))
assert not gate([F("modified_after_start", carpeta)])[0]["ok"]  # una carpeta vieja no cuenta como hecha

# --- ventanas y página falsas ---
W = [{"uuid": "uuid-kcalc", "resource_class": "org.kde.kcalc", "caption": "KCalc — 777"},
     {"uuid": "uuid-kwrite", "resource_class": "org.kde.kwrite", "caption": "informe.txt — KWrite"}]
res = gate([{"kind": "window", "check": "exists"}, {"kind": "window", "check": "title_regex", "value": "777"},
            {"kind": "window", "check": "exists", "resource_class": "org.kde.dolphin"},
            {"kind": "window", "check": "title_regex", "resource_class": "org.kde.kwrite", "value": r"^informe"},
            {"kind": "window", "check": "title_regex", "uuid": "uuid-kwrite", "value": "nada"}], windows=W)
assert [r["ok"] for r in res] == [True, True, False, True, False], res
assert "desktop_state" in res[2]["next"] and "KWrite" not in json.dumps(res), res
# resource_class corto, como `_window` del servidor: el exacto manda, luego el último tramo sin mayúsculas
W2 = [*W, {"uuid": "uuid-dolphin", "resource_class": "org.kde.dolphin", "caption": "home — Dolphin"},
      {"uuid": "uuid-alta", "resource_class": "manos-alta", "caption": "Alta de socio"}]
C = lambda name: {"kind": "window", "check": "exists", "resource_class": name}
res = gate([C("dolphin"), C("Dolphin"), C("org.kde.dolphin"), C("manos-alta"), C("kde.dolphin"), C("org"),
            C("dolph")], windows=W2)
assert [r["ok"] for r in res] == [True, True, True, True, False, False, False], res
assert [w["uuid"] for w in verify.by_class(W2, "kcalc")] == ["uuid-kcalc"]
assert verify.by_class(W2, "") == [] and verify.by_class(W2, None) == []
X = [{"uuid": "a", "resource_class": "kwrite"}, {"uuid": "b", "resource_class": "org.kde.kwrite"}]
assert [w["uuid"] for w in verify.by_class(X, "kwrite")] == ["a"]  # el exacto gana al corto


class Page:
    url = "http://127.0.0.1:8000/gracias.html?token=abc"

    async def evaluate(self, js, arg=None):
        if arg is None:
            assert ".slice(0, 200000)" in js, js  # el texto llega recortado a 200 KB
            return "¡Gracias! Pedido 12345 confirmado"
        if arg == "#mal[":
            raise RuntimeError("SyntaxError")
        return {"#ok": 1, ".error": 0}.get(arg, 0)


B = lambda check, value: {"kind": "browser", "check": check, "value": value}
nav = verify.mint("browser_observe", "uuid-chrome", Page.url)
res = gate([B("selector_present", "#ok"), B("selector_absent", ".error"), B("text_regex", r"Pedido \d+"),
            B("url_regex", r"gracias\.html"), B("selector_present", ".error"), B("selector_absent", "#ok"),
            B("text_regex", "Error"), B("url_regex", "^https:"), B("selector_present", "#mal[")], page=Page(), oid=nav)
assert [r["ok"] for r in res] == [True] * 4 + [False] * 5, res
assert "12345" not in json.dumps(res) and "abc" not in json.dumps(res) and "no vale" in res[8]["observed"], res
# atadas a la página observada: otra observación o otra url → observa de nuevo
assert "observa de nuevo" in raises(verify.BadEvidence, gate, [B("selector_present", "#ok")], page=Page())
moved = verify.mint("browser_observe", "uuid-chrome", "http://127.0.0.1:8000/form.html")
assert "observa de nuevo" in raises(verify.BadEvidence, gate, [B("selector_present", "#ok")], page=Page(), oid=moved)
assert gate([B("selector_present", "#ok"), B("url_regex", "gracias")], page=Page(), oid=moved)[0]["ok"]

# --- unidades systemd: el nombre se valida; una que no existe no está activa (systemctl --user is-active, solo lee)
assert raises(verify.BadEvidence, gate, [{"kind": "process", "check": "systemd_unit_active", "unit": "x; rm -rf /"}])
res = gate([{"kind": "process", "check": "systemd_unit_active", "unit": "manos-no-existe-x7.service"}])
assert not res[0]["ok"] and res[0]["observed"] in ("inactive", "unknown", "sin respuesta"), res

# --- lo que se rechaza sin comprobar nada ---
nope = lambda asserts, **kw: raises(verify.BadEvidence, gate, asserts, **kw)
assert "Red, código de salida" in nope([{"kind": "network", "check": "status"}])
assert nope([{"kind": ["file"], "check": "exists"}]) and nope([{"kind": {"a": 1}, "check": "exists"}])  # no hashable
assert nope([{"kind": "file", "check": ["exists"]}]) and nope(["file"])
assert nope([{"kind": "process", "check": "exit_code", "cmd": "true"}])
assert nope([{"kind": "file", "check": "exists", "path": "relativo.txt"}])
assert nope([F("sha256", value="abc")]) and nope([F("regex", value="(")])
for v in (0, -1, True, False, 1.5, "10"):  # min_size: entero ≥ 1, y un bool no es un entero
    assert nope([F("min_size", value=v)]), v
for rx in ("", ".*", "^", "a*", "(x)?", "$|y", "x|"):  # casan con el vacío: no pueden fallar
    assert "no puede fallar" in nope([F("regex", value=rx)]) or not rx, rx
assert "no puede fallar" in nope([{"kind": "window", "check": "title_regex", "value": ".*"}])
for p in ("/proc/self/environ", "/sys/kernel/x", "/dev/zero", str(Path.home() / ".ssh/id_ed25519"),
          str(PROFILE / "Default/Cookies"), str(Path.home() / ".config/gh/hosts.yml"),
          str(Path.home() / ".gnupg/pubring.kbx"), str(Path.home() / ".local/share/kwalletd/kdewallet.kwl"),
          str(Path.home() / ".claude/.credentials.json"), str(Path.home() / ".config/BraveSoftware/x")):
    assert "no lee" in nope([F("exists", p)]), p
os.symlink("/proc/self/status", tmp / "enlace")  # un enlace no la salta
os.symlink(Path.home() / ".config/kwinrc", tmp / "enlace-dot")  # ni hacia un fichero oculto de la casa
assert "no lee" in nope([F("regex", tmp / "enlace-dot", value="x")])
assert "no lee" in nope([F("regex", tmp / "enlace", value="Pid")])
assert nope([{"kind": "browser", "check": "selector_present"}])
assert nope([{"kind": "window", "check": "exists"}], oid=verify.mint("browser_observe", "", "http://x"))
assert nope([{"kind": "window", "check": "exists"}], oid=verify.mint("desktop_state", "uuid-activa"))  # sin ventana
assert gate([{"kind": "window", "check": "exists", "uuid": "uuid-kcalc"}], windows=W,
            oid=verify.mint("desktop_state"))[0]["ok"]  # con diana explícita, cualquier observación
assert "olvidado" in nope([F("exists")], oid="obs-000000000000")
assert "olvidado" in nope([F("exists")], oid=["no", "hashable"])
stale = verify.mint("ui_snapshot", "uuid-kcalc")
verify._obs[stale]["t"] -= verify.MAX_AGE + 1
assert "30 min" in nope([F("exists")], oid=stale)
assert raises(verify.NoSuchTask, gate, [F("exists")], task="otra")
verify.checkpoint("sin-accept", ["a"], [], "")
assert "task_checkpoint(accept=" in nope([F("exists")], task="sin-accept", oid=verify.mint("ui_snapshot", "u"))
verify.checkpoint("nueva", ["a"], [], "", ACCEPT)
assert "antes de empezar" in nope([F("exists")], task="nueva")  # `obs` se acuñó antes de abrir "nueva"
assert all(verify.mint("x") for _ in range(verify.MAX_OBS + 5))
assert len(verify._obs) == verify.MAX_OBS and obs not in verify._obs  # el más viejo se olvida

# --- finish: solo en verde marca la tarea hecha; una hecha no se vuelve a cerrar; reusar el nombre la reabre ---
t = verify.resume("informe-1")
out = verify.finish("informe-1", t["started"], [{"ok": True}, {"ok": False, "i": 1}], "obs-a")
assert out["status"] == "open" and out["passed"] == 1 and verify.resume("informe-1")["status"] == "open", out
assert "cambió" in raises(verify.BadEvidence, verify.finish, "informe-1", t["started"] - 1, [{"ok": True}], "obs-a")
out = verify.finish("informe-1", t["started"], [{"ok": True}], "obs-a")
assert out == {"status": "done", "passed": 1, "failed": [], "revisions": 0} and \
    verify.resume("informe-1")["status"] == "done"
assert raises(verify.BadEvidence, verify.finish, "informe-1", t["started"], [{"ok": True}], "obs-a")
fresh = verify.mint("ui_snapshot", "uuid-kcalc")
assert "ya está terminada" in nope([F("exists")], oid=fresh)
time.sleep(0.01)
again = verify.checkpoint("informe-1", [], [], "otra vuelta")
assert again["status"] == "open" and again["done"] == [] and again["started"] > t["started"], again
assert "evidence" not in again and again["accept"] == ACCEPT and again["plan"] == t["plan"], again
assert "antes de empezar" in nope([F("exists")], oid=fresh)  # lo observado en la vuelta anterior ya no vale

# --- revise: un criterio mal puesto se corrige o se quita con motivo; queda apuntado y deja de correr ---
WHY = "acaba en .txt, así que su sitio es Textos y no PDF"
mal, bien = F("exists", tmp / "PDF/informe.pdf.txt"), F("exists", tmp / "Textos/informe.pdf.txt")
A2 = [mal, F("modified_after_start", tmp / "Textos")]
verify.checkpoint("ordenar", ["mover"], [], "", A2)
rev = lambda *revs: verify.checkpoint("ordenar", [], [], "", None, list(revs))
R = lambda i, a, reason=WHY: {"i": i, "assertion": a, "reason": reason}
for bad in ({"i": 0, "assertion": bien}, R(0, bien, "estaba mal"), R(0, bien, " " * 40), R(0, bien, None)):
    assert "`reason`" in raises(verify.BadEvidence, rev, bad), bad  # sin motivo, o corto
assert "índices" in raises(verify.BadEvidence, rev, R(2, bien)) and raises(verify.BadEvidence, rev, R(-1, bien))
assert raises(verify.BadEvidence, rev, R(0, {"kind": "file", "check": "exists"}))  # el mismo motor valida
assert raises(verify.BadEvidence, rev, R(0, F("regex", value=".*")))
assert "una vez" in raises(verify.BadEvidence, rev, R(0, bien), R(0, None))
assert "sin criterios" in raises(verify.BadEvidence, rev, R(0, None), R(1, None))  # el último no se quita
assert verify.resume("ordenar")["accept"] == A2 and "revisions" not in verify.resume("ordenar")  # nada cambió
t = rev(R(0, bien))
assert t["accept"] == [bien, A2[1]] and t["revisions"] == [{"ts": t["revisions"][0]["ts"], "i": 0, "old": mal,
                                                             "new": bien, "reason": WHY}], t
t = rev(R(1, None))
assert t["accept"] == [bien] and t["revisions"][1] | {"ts": 0} == {"ts": 0, "i": 1, "old": A2[1], "new": None,
                                                                    "reason": WHY}, t
assert "sin criterios" in raises(verify.BadEvidence, rev, R(0, None))
# `accept` sigue solo creciendo, ahora sobre la lista corregida
assert "solo puede crecer" in raises(verify.BadEvidence, verify.checkpoint, "ordenar", [], [], "", A2)
(tmp / "Textos").mkdir()
(tmp / "Textos/informe.pdf.txt").write_text("x")
tk, ob, a = verify.validate("ordenar", {"observation_id": verify.mint("ui_snapshot", "u"), "assertions": []})
res = asyncio.run(verify.run(tk, ob, a))
assert [r["target"] for r in res] == [str(tmp / "Textos/informe.pdf.txt")] and res[0]["ok"], res  # el quitado no corre
assert verify.finish("ordenar", tk["started"], res, "obs-a") == {"status": "done", "passed": 1, "failed": [],
                                                                 "revisions": 2}
# por la tool: una línea de auditoría por corrección, con el índice y sin el motivo ni la aserción
verify.checkpoint("ordenar-2", ["mover"], [], "", A2)
out = asyncio.run(server.task_checkpoint("ordenar-2", revise=[R(0, bien), R(1, None)]))
assert out["ok"] and out["accept"] == [bien], out
assert not asyncio.run(server.task_checkpoint("ordenar-2", revise=[R(0, None, "corto")]))["ok"]  # no se audita
lines = [json.loads(l) for l in (tmp / "data/manos/audit.jsonl").read_text().splitlines()]
assert [(l["tool"], l["action"], l["i"], l["acting"]) for l in lines] == [("task_checkpoint", "revise", 0, False),
                                                                         ("task_checkpoint", "revise", 1, False)], lines
text = (tmp / "data/manos/audit.jsonl").read_text()
assert "Textos" not in text and "PDF" not in text and "corto" not in text, text

# --- detector de bucles ---
fp = verify.fingerprint
assert fp("window_set", {"a": 1, "b": [2]}) == fp("window_set", {"b": [2], "a": 1}) != fp("window_set", {"a": 2})
assert fp("ui_act", (5, "press", "")) == fp("ui_act", [5, "press", ""])
OK = lambda **kw: {"ok": True, "error": None, **kw}
ERR = lambda t: {"ok": False, "error": {"type": t, "message": "", "next": ""}}


def act(tool, args, result, target="w"):
    """Lo que hace el servidor alrededor de una acción: guard, acción (si pasa), record."""
    f_ = fp(tool, args)
    try:
        verify.guard(tool, f_)
    except verify.Stuck as e:
        verify.record(tool, f_, target, ERR("Stuck"))
        return "Stuck", str(e)
    return "done", verify.record(tool, f_, target, dict(result))


# mismo resultado tres veces → la tercera no se hace; `before` y los tiempos no cuentan
g = lambda before: OK(geometry=[0, 0, 500, 500], before=before, ms=time.time())
assert act("window_set", ["k", [0, 0, 500, 500]], g([9, 9, 9, 9]))[0] == "done"
assert act("window_set", ["k", [0, 0, 500, 500]], g([0, 0, 500, 500]))[0] == "done"
kind, msg = act("window_set", ["k", [0, 0, 500, 500]], g([0, 0, 500, 500]))
assert kind == "Stuck" and "desktop_state" in msg, msg
assert act("window_set", ["k", [0, 0, 500, 500]], g([0, 0, 500, 500]))[0] == "Stuck"  # sigue atascado
assert act("window_set", ["k", None], OK(geometry=[1, 1, 1, 1]))[0] == "done"  # otra acción en medio: se suelta
assert act("window_set", ["k", [0, 0, 500, 500]], g(None))[0] == "done"
# idénticas con el estado cambiando cada vez: reintentos legítimos
for i in range(5):
    assert act("app_invoke", ["k", "show_history"], OK(after={"checked": i % 2 == 0}))[0] == "done"
# app_invoke que acierta con el mismo resultado (zoom, siguiente): nunca Stuck; dos errores iguales sí
for _ in range(4):
    assert act("app_invoke", ["k", "view_zoom_in"], OK(after={"enabled": True, "checked": None}))[0] == "done"
assert [act("app_invoke", ["k", "roto"], ERR("ActionDisabled"))[0] for _ in range(3)] == ["done", "done", "Stuck"]
# ui_act: el estado lo da `before` (el árbol AX antes de actuar), no su resultado
U = ["ui_act", (7, "press", "")]
for tree in ("t1", "t2", "t3"):  # el visor cambia en cada pulsación: nunca Stuck
    f_ = fp(*U)
    if verify.repeated(f_):
        verify.guard("ui_act", f_, tree)
    verify.record("ui_act", f_, 7, OK(after={"role": "button"}))
f_ = fp(*U)
verify.guard("ui_act", f_, "t4")
verify.record("ui_act", f_, 7, OK(after={"role": "button"}))
assert raises(verify.Stuck, verify.guard, "ui_act", f_, "t4")  # la anterior no cambió nada: esta se rechaza
assert raises(verify.Stuck, verify.guard, "ui_act", f_, "t4")  # y la siguiente vuelve a leer y sigue igual...
assert not raises(verify.Stuck, verify.guard, "ui_act", f_, "t5")  # ...hasta que la ventana cambia: se hace
assert not raises(verify.Stuck, verify.guard, "ui_act", f_, None)  # sin árbol legible no se rechaza
# Busy y Cancelled no cuentan: no se hizo nada
verify._tail.clear()
for _ in range(4):
    assert act("app_launch", ["org.kde.kwrite"], ERR("Busy"))[0] == "done"
assert not verify._tail

# FailureTracker: pista al 2º fallo seguido por (tool, diana); cualquier acierto lo borra; caduca a los 5 min
assert act("app_invoke", ["k", "x1"], ERR("NoSuchAction"), "k")[1] is None
hint = act("app_invoke", ["k", "x2"], ERR("NoSuchAction"), "k")[1]
assert hint and hint.startswith("2 fallos seguidos de app_invoke en k") and "ui_snapshot" in hint, hint
assert act("app_invoke", ["j", "x2"], ERR("NoSuchAction"), "j")[1] is None  # otra diana, otra cuenta
assert act("window_set", ["z", None], OK(geometry=[2, 2, 2, 2]), "z")[1] is None  # un acierto, de otra tool
assert act("app_invoke", ["k", "x3"], ERR("NoSuchAction"), "k")[1] is None  # reseteado
verify._fails[("app_invoke", "k")] = (1, time.time() - verify.FAIL_TTL - 1)
assert act("app_invoke", ["k", "x4"], ERR("NoSuchAction"), "k")[1] is None  # el de hace 5 min ya no cuenta
assert act("form_fill", [[1]], ERR("FormError"), "")[1] is None
assert act("form_fill", [[2]], ERR("FormError"), "")[1].startswith("2 fallos seguidos de form_fill: vuelve a form_describe")
r = {"ok": True, "error": None, "state": 123}
verify.record("browser_dialog", fp("browser_dialog", [True, ""]), "", r)
assert "state" not in r  # `state` solo alimenta el hash, no sale al modelo

print("test_verify OK")
