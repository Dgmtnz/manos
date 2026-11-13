"""Autocomprobación pura de la mitad de escritorio:  uv run --project . tests/test_desktop.py

Disciplina D-Bus (docs/p3-design.md §1): toda llamada a un nombre que vive en kwin_wayland sale de
`kwin.py`, con método y firma literales y en `ALLOWLIST`. Una llamada a mano con una firma mal hecha
abortó KWin y cerró la sesión entera; meter una entrada nueva aquí obliga a revisarla antes.
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import kwin

# (interfaz, método, firma) que se pueden llamar. Exactamente lo que kwin.py llama hoy.
ALLOWLIST = {("org.kde.krunner1", "Match", "s"), ("org.kde.KWin", "getWindowInfo", "s"),
             ("org.kde.kwin.Scripting", "loadScript", "ss"), ("org.kde.kwin.Script", "run", ""),
             ("org.kde.kwin.Scripting", "unloadScript", "s"), ("org.kde.kwin.Scripting", "isScriptLoaded", "s"),
             ("org.kde.KWin.EIS.RemoteDesktop", "connectToEIS", "i"),
             ("org.kde.KWin.EIS.RemoteDesktop", "disconnect", "i"),
             ("org.kde.KWin.ScreenShot2", "CaptureWorkspace", "a{sv}h"),
             # desktop/atspi.py, con servicio literal (docs/p3-design.md §1): bus de accesibilidad y registro
             ("org.a11y.Bus", "GetAddress", ""), ("org.freedesktop.DBus.Peer", "Ping", ""),
             ("org.freedesktop.DBus.Properties", "Set", "ssv")}
# Lado de la app (desktop/apps.py): el servicio es el de la ventana y no es literal, así que una llamada con
# servicio variable solo vale fuera de kwin.py y solo con una de estas. El dueño nunca es kwin ni plasmashell.
APP_ALLOWLIST = {("org.freedesktop.DBus", "ListNames", ""), ("org.freedesktop.DBus", "GetConnectionUnixProcessID", "s"),
                 ("org.freedesktop.DBus.Introspectable", "Introspect", ""),
                 ("org.freedesktop.DBus.Properties", "Get", "ss"), ("org.freedesktop.DBus.Properties", "GetAll", "s"),
                 ("org.kde.KMainWindow", "actions", ""), ("org.kde.KMainWindow", "actionIsEnabled", "s"),
                 ("org.kde.KMainWindow", "activateAction", "s")}
VERBS = {"call", "status"}  # lo único que _busctl puede pedir
BANNED = {"get_object", "Interface", "get_dbus_method", "dbus_call"}
CLIS = {"busctl", "gdbus", "qdbus", "qdbus6", "dbus-send"}
KWIN_NAMES = re.compile(r"org\.kde\.KWin|kglobalaccel|org\.kde\.keyboard|ScreenSaver|screensaver")
# dbus-python: call_blocking/call_async(bus_name, object_path, dbus_interface, method, signature, ...)
PARAMS = ["bus_name", "object_path", "dbus_interface", "method", "signature"]


def lit(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def violations(src: str, name: str, in_kwin: bool) -> list[str]:
    bad, tree = [], ast.parse(src)
    # la única lista de CLI permitida es la de dentro de `_busctl` en kwin.py
    inner = {id(n) for f in ast.walk(tree) if in_kwin and isinstance(f, ast.FunctionDef) and f.name == "_busctl"
             for n in ast.walk(f)}
    for node in ast.walk(tree):
        where = f"{name}:{getattr(node, 'lineno', '?')}"
        if not in_kwin and lit(node) and KWIN_NAMES.search(node.value):
            bad.append(f"{where} nombre de KWin fuera de kwin.py: {node.value!r}")
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and lit(node.elts[0]) in CLIS and id(node) not in inner:
            bad.append(f"{where} herramienta de bus por CLI sin pasar por _busctl")
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fn = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if fn in BANNED:
            bad.append(f"{where} {fn}() prohibido")
        elif fn in ("call_blocking", "call_async"):
            a = dict(zip(PARAMS, node.args)) | {k.arg: k.value for k in node.keywords if k.arg}
            vals = [lit(a.get(p)) for p in ("bus_name", "dbus_interface", "method", "signature")]
            ok = ALLOWLIST | APP_ALLOWLIST if vals[0] else set() if in_kwin else APP_ALLOWLIST
            if None in vals[1:] or tuple(vals[1:]) not in ok:
                bad.append(f"{where} {fn} no literal o fuera de ALLOWLIST")
        elif fn == "_busctl":
            args = [lit(x) for x in node.args]
            opts = 0
            while opts < len(args) and args[opts] and args[opts].startswith("--"):
                opts += 1
            if opts == len(args) or args[opts] is None:
                bad.append(f"{where} _busctl con orden no literal")
            elif args[opts] not in VERBS:
                bad.append(f"{where} _busctl {args[opts]!r}: solo {sorted(VERBS)}")
            elif args[opts] == "call":
                rest = args[opts + 1:]  # servicio, ruta, interfaz, método, firma
                if len(rest) < 4 or None in (rest[0], rest[2], rest[3]) or (len(rest) > 4 and rest[4] is None) \
                        or (rest[2], rest[3], rest[4] if len(rest) > 4 else "") not in ALLOWLIST:
                    bad.append(f"{where} _busctl call no literal o fuera de ALLOWLIST")
    return bad


# El comprobador tiene que saltar: cada uno de estos debe dar exactamente una violación.
for src, kw in [('bus.get_object("org.kde.KWin", "/")', True),
                ('bus.call_blocking("a.b", "/", "a.b", "Hack", "ai", [1])', True),
                ('bus.call_blocking("a.b", "/", "a.b", m, "s", [1])', True),
                ('_busctl("call", "a.b", "/", "a.b", "Match", sig, x)', True),
                ('_busctl("--json=short", "call", "a.b", "/", "a.b", "Hack", "s", x)', True),
                ('_busctl(*args)', True),
                ('subprocess.run(["gdbus", "call", "-e"])', True),
                ('subprocess.run(["busctl", "--user", "tree"])', True),
                ('bus.call_blocking("a.b", "/", "x.y", "Match", "s", [1])', True),
                ('bus.call_blocking(svc, "/", "org.kde.KMainWindow", "actions", "", ())', True),
                ('bus.call_blocking(svc, "/", "org.kde.kwin.Scripting", "loadScript", "ss", (p, n))', False),
                ('_busctl("call", "a.b", "/", "x.y", "Match", "s", x)', True),
                ('_busctl("emit", "/", "a.b", "Sig")', True),
                ('X = "org.kde.kglobalaccel"', False)]:
    assert len(violations(src, "<demo>", kw)) == 1, (src, violations(src, "<demo>", kw))
assert not violations('bus.call_blocking(svc, p, "org.kde.KMainWindow", "activateAction", "s", (a,))', "<demo>", False)
for ok in ['_busctl("call", "a.b", f"/S{i}", "org.kde.kwin.Script", "run")', '_busctl("status", "a.b")',
           'def _busctl(*a):\n    subprocess.run(["busctl", BUS, *a])']:
    assert not violations(ok, "<demo>", True), (ok, violations(ok, "<demo>", True))
assert violations('def _busctl(*a):\n    subprocess.run(["busctl", BUS, *a])', "<demo>", False)

files = [ROOT / "kwin.py", *sorted((ROOT / "desktop").glob("*.py"))]
bad = [v for p in files for v in violations(p.read_text(), p.name, p.name == "kwin.py")]
assert not bad, "\n".join(bad)
print(f"{len(files)} ficheros sin violaciones")

# G5 y el veto de run_js, sin bus: el "diario" es un echo y el veto salta antes de cargar nada. Por si el
# veto fallara, BUS apunta a un socket que no existe y el script no llega a ninguna KWin.
kwin.BUS = "--address=unix:path=/nonexistent/manos-test"


def reply(obj):
    kwin._JOURNAL = ["echo", "MANOS_t " + json.dumps(obj)]
    try:
        return kwin._read_reply("MANOS_t", 0)
    except kwin.KWinError:
        return "G5"


ok = {"uuid": "ecd47cfe-7cb4-49c3-938f-e9ccf3f39261", "rc": "org.kde.kcalc", "out": ["eDP-1", "HDMI-A-1"],
      "id": "{204bd654-b5f9-4130-a8bd-f6adc4e9b488}", "n": 3.5, "b": True, "x": None}
assert reply(ok) == ok
for bad in [{"caption": "a b"}, {"w": [{"caption": "Contraseña: hunter2"}]}, {"a b": 1}, {"x": "y" * 65},
            {"x": "Contraseña"}]:  # sin re.ASCII, \w dejaba pasar texto libre de una palabra
    assert reply(bad) == "G5", bad
for body in ['callDBus("a", "/", "a", "M")', 'registerShortcut("x", "x", "Meta+X", f)', 'var k = "kglobalaccel";']:
    try:
        kwin.run_js(body)
        raise AssertionError(body)
    except kwin.KWinError as e:
        assert "rechazado" in str(e), e  # y no el busctl fallido contra el socket inexistente

# shell_pids falla cerrado en vivo, y bus_address cae al socket estándar sin DBUS_SESSION_BUS_ADDRESS. Sin bus:
# _busctl es una función falsa y nada se conecta.
import os
busctl, aimed = kwin._busctl, kwin.BUS
kwin._busctl, kwin.BUS = (lambda *a, **k: ""), "--user"
env = os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
try:
    try:
        kwin.shell_pids()
        raise AssertionError("shell_pids vacío en vivo y sin error")
    except kwin.KWinError:
        pass
    assert kwin.bus_address() == f"unix:path={os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'}/bus"
finally:
    kwin._busctl, kwin.BUS = busctl, aimed
    if env is not None:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = env

# El candado del escritorio por el protocolo, con el hilo falso (ni EIS ni AT-SPI): dos ui_act a la vez van por
# turnos; tras LOCK_WAIT, Busy; cancelar una llamada que espera no toca eis.ABORT ni a la que tiene el candado.
import asyncio
import tempfile
import threading
import time

import anyio
from mcp import Client

import server
from browser import session as bs
from desktop import eis

os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="manos-desk-")  # la auditoría, fuera de la real
spans, seen = [], threading.Lock()


def fake(audit, ref, action, value):
    t = time.monotonic()
    time.sleep(0.3)
    with seen:
        spans.append((ref, t, time.monotonic(), eis.ABORT.is_set()))
    return {"via": "fake"}


async def busy_checks():
    server._ui_act = fake
    async with Client(server.mcp) as c:
        async def act(ref):
            return (await c.call_tool("ui_act", {"ref": ref, "action": "press"})).structured_content

        rs = await asyncio.gather(act(1), act(2))
        assert all(r["ok"] for r in rs), rs
        (_, _, end, _), (_, start, _, _) = sorted(spans, key=lambda x: x[1])
        assert end <= start, spans  # por turnos, no solapadas

        bs.LOCK_WAIT = 0.1
        rs = await asyncio.gather(act(3), act(4))
        bs.LOCK_WAIT = 10.0
        assert sorted(r["ok"] for r in rs) == [False, True], rs
        busy = next(r for r in rs if not r["ok"])["error"]
        assert busy["type"] == "Busy" and "10 s" in busy["next"], busy

        holder = asyncio.ensure_future(act(5))
        while not server._desk_lock.locked():
            await asyncio.sleep(0.01)
        with anyio.move_on_after(0.1):
            await act(6)
            raise AssertionError("la llamada que esperaba no se canceló")
        r = await holder
        await asyncio.sleep(0.5)  # si la cancelada hubiera cogido el candado, ya habría corrido
        assert r["ok"] and not eis.ABORT.is_set(), r
        assert [x[0] for x in spans] == [1, 2, 3, 5] or [x[0] for x in spans] == [1, 2, 4, 5], spans
        assert not any(x[3] for x in spans), spans  # nadie vio ABORT
        assert not server._desk_lock.locked()


asyncio.run(busy_checks())
print("candado del escritorio: por turnos, Busy tras LOCK_WAIT, cancelar la espera no toca a quien lo tiene")
print("test_desktop OK")
