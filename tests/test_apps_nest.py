"""Peldaño 1 del escritorio y sus tools, contra una KWin anidada:  uv run --project . tests/test_apps_nest.py

N8: kcalc por KXmlGui: app_actions lista sus acciones; una casilla (show_history) se marca y se desmarca con
app_invoke; una acción que abre ventana la devuelve en `new_windows`; un pid del shell (la KWin anidada) da
Refused; una acción que no existe da NoSuchAction con alternativas; una destructiva sin approved, NeedsApproval.
N9: app_launch org.kde.kwrite en ≤20 s, al frente y aparcada (el domador se descarga al verla).
Humo del servidor por `mcp.call_tool` (valida la salida contra el outputSchema): desktop_state, window_set y
screen_capture con sus formas; ventana sensible → Refused y SensitiveWindowOpen; candado cogido → Busy; la
auditoría escribe en el XDG_DATA_HOME de la anidada, con sus 9 claves y sin ningún título.
Por el protocolo (`mcp.Client` en memoria): list_tools da las 30 tools, y cada una de escritorio con outputSchema.
"""

import asyncio
import base64
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client
from PIL import Image

import kwin
import server
import spaces
from browser import session as bs
from desktop import apps
from nest import nested

CORES = lambda: subprocess.run(["coredumpctl", "list", "--no-legend"], capture_output=True, text=True).stdout
CORES0 = CORES().count("kwin_wayland")
LIVE_AUDIT = Path.home() / ".local/share/manos/audit.jsonl"
LIVE_AUDIT0 = LIVE_AUDIT.exists() and LIVE_AUDIT.stat().st_size

# Puro: el emparejador de sensibles y el de destructivas
assert all(map(server.sensitive, ["org.kde.polkit-kde-authentication-agent-1", "org.kde.kwalletd6", "pinentry-qt",
                                  "org.keepassxc.KeePassXC", "ksshaskpass", "org.kde.ksecretd"]))
assert not any(map(server.sensitive, ["org.kde.kcalc", "", None]))
assert all(map(apps.DESTRUCTIVE.search, ["deletefile", "move_to_trash_delete", "empty_trash", "file_revert", "discard"]))
assert not apps.DESTRUCTIVE.search("file_save")


async def call(tool, **args) -> dict:
    """Por el SDK, como lo llamaría el cliente: la salida se valida contra el outputSchema."""
    r = await server.mcp.call_tool(tool, args)
    return r.structured_content


def wait_class(rc, n=1):
    deadline = time.monotonic() + 20
    while len(ws := [w for w in kwin.state()["windows"] if w["resource_class"] == rc]) < n:
        assert time.monotonic() < deadline, f"{rc} no apareció"
        time.sleep(0.1)
    return ws


launched = []  # kstart sin systemd1 en el bus anidado hace fork: kwrite queda en el cgroup de quien llama


DESKTOP = {"desktop_state", "app_actions", "app_invoke", "window_set", "app_launch", "screen_capture",
           "ui_snapshot", "ui_act"}


async def main(n):
    async with Client(server.mcp) as c:  # lo que ve un cliente de verdad, serializado
        tools = {t.name: t for t in (await c.list_tools()).tools}
    print(f"list_tools: {len(tools)}:", " ".join(sorted(tools)))
    assert len(tools) == 30 and "browser_download" in tools and DESKTOP <= set(tools), sorted(tools)
    assert all(tools[t].output_schema for t in DESKTOP), [t for t in DESKTOP if not tools[t].output_schema]

    n.launch(["kcalc"])
    kcalc = wait_class("org.kde.kcalc")[0]

    # N8
    r = await call("app_actions", window="org.kde.kcalc")
    acts = {a["name"]: a for a in r["actions"]}
    assert r["ok"] and r["object"] == "/kcalc/MainWindow_1" and r["total"] == len(acts) >= 20, r
    assert acts["show_history"] | {"text": ""} == {"name": "show_history", "text": "", "enabled": True,
                                                    "checkable": True, "checked": False}, acts["show_history"]
    assert acts["help_about_app"]["checkable"] is None, acts["help_about_app"]  # sin nodo: solo enabled
    r = await call("app_actions", window=kcalc["uuid"], find="HISTORY", max=1)
    assert r["total"] == 1 and r["actions"][0]["name"] == "show_history", r
    for want in (True, False):
        r = await call("app_invoke", window="org.kde.kcalc", action="show_history")
        assert r["ok"] and r["invoked"] is True and r["new_windows"] == [], r
        assert r["before"] == {"enabled": True, "checked": not want} and r["after"] == {"enabled": True, "checked": want}, r
    r = await call("app_invoke", window="active", action="show_histroy")
    assert r["error"]["type"] == "NoSuchAction" and "show_history" in r["error"]["available"], r
    r = await call("app_invoke", window="org.kde.kcalc", action="edit_delete")
    assert r["error"]["type"] == "NeedsApproval", r
    r = await call("app_invoke", window="org.kde.kcalc", action="options_configure")
    assert r["ok"] and r["invoked"] is True and len(r["new_windows"]) == 1, r
    kwin.window_set(r["new_windows"][0]["uuid"], minimized=True)  # que no tape a kcalc en las capturas
    for pid in (n.pid, *kwin.shell_pids()):
        try:
            apps.actions({"pid": pid, "caption": ""})
            raise AssertionError("acciones de un pid del shell")
        except apps.Refused:
            pass

    # N9
    r = await call("app_launch", app="org.kde.kwrite")
    assert r["ok"] and 0 < r["ms"] <= 20000 and kwin.active() == r["window"], r
    launched.append(r["pid"])
    cgroup = Path(f"/proc/{r['pid']}/cgroup").read_text().strip()
    print(f"N9: kwrite al frente en {r['ms']} ms; cgroup {cgroup.rsplit('/', 1)[-1]}")
    front = r["window"]
    r = await call("app_launch", app="org.kde.kwrite", target="parked")
    assert r["ok"], r
    launched.append(r["pid"])
    assert kwin.window_info(r["window"])["minimized"] and not any(map(kwin.is_loaded, ["manoslaunch0", "manoslaunch1"])), r
    info = kwin.window_info(r["window"])  # aparcada es solo minimizada: vuelve a la barra y a las capturas
    parked = next(w for w in kwin.state()["windows"] if w["uuid"] == r["window"])
    assert "skipTaskbar" in info and not parked["exclude_from_capture"] and not any(info.get(k) for k in ("skipTaskbar", "skipSwitcher", "skipPager")), info
    r = await call("app_launch", app="org.kde.kwrit")
    assert r["error"]["type"] == "NotInstalled" and "org.kde.kwrite" in r["error"]["available"], r
    r = await call("app_launch", app="--help")
    assert r["error"]["type"] == "NotInstalled", r

    # desktop_state (desde aquí kcalc por uuid: su diálogo de configuración también es org.kde.kcalc)
    K = kcalc["uuid"]
    st = await call("desktop_state")
    assert st["ok"] and st["outputs"][0]["width"] == 1280 and st["virtual_screen"] == [1280, 800], st
    assert st["locked"] is False and st["kill_switch"] is False and isinstance(st["a11y"], bool), st
    k = next(w for w in st["windows"] if w["uuid"] == kcalc["uuid"])
    assert k["sensitive"] is False and k["caption"] and k["pid"] == kcalc["pid"], k
    assert set((await call("desktop_state", include=["outputs"])).keys()) >= {"outputs", "active", "cursor"}

    # window_set: geometría releída, clamp de KWin y activar
    r = await call("window_set", window=K, geometry=[40, 50, 520, 600], active=True)
    assert r["ok"] and r["geometry"] == [40, 50, 520, 600] and not r["clamped"] and r["active"], r
    r = await call("window_set", window=K, geometry=[40, 50, 10, 10])
    assert r["ok"] and r["clamped"] and r["before"] == [40, 50, 520, 600], r
    r = await call("window_set", window="org.kde.kcalcx")
    assert r["error"]["type"] == "NotFound" and "org.kde.kcalc" in r["error"]["available"], r
    r = await call("window_set", window="org.kde.kcalc")  # kcalc y su diálogo de configuración
    assert r["error"]["type"] == "AmbiguousWindow" and len(r["error"]["available"]) == 2, r

    # screen_capture: imagen + estructurado por el SDK
    res = await server.mcp.call_tool("screen_capture", {"target": "screen", "max_edge": 640})
    sc = res.structured_content
    assert sc["ok"] and (sc["width"], sc["height"]) == (640, 400) and sc["tokens"] == spaces.tokens(640, 400), sc
    img = Image.open(io.BytesIO(base64.b64decode(res.content[0].data)))
    assert res.content[0].mime_type == "image/jpeg" and img.size == (640, 400), res.content[0].mime_type
    assert json.loads(res.content[1].text) == sc
    sc = await call("screen_capture", target=K)
    assert sc["ok"] and sc["backend"] == "screenshot2", sc
    kwin.window_set(K, excludeFromCapture=True)  # como la ventana del navegador del agente: saldría negra
    try:
        sc = await call("screen_capture", target=K)
        assert sc["error"]["type"] == "NotVisible", sc
    finally:
        kwin.window_set(K, excludeFromCapture=False)

    # una ventana del shell se niega en todo menos la captura
    shell = kwin.shell_pids
    kwin.shell_pids = lambda: {kcalc["pid"]}
    try:
        for tool, args in [("window_set", {"window": K, "active": True}), ("app_actions", {"window": K}),
                           ("ui_snapshot", {"window": K})]:
            assert (await call(tool, **args))["error"]["type"] == "Refused", tool
        assert (await call("screen_capture", target=K))["ok"]
    finally:
        kwin.shell_pids = shell

    # una acción que cierra la app no es un fallo: after None (o lo que alcanzó a leer antes de cerrarse)
    r = await call("app_invoke", window=front, action="file_quit")
    assert r["ok"] and r["invoked"] in (True, None), r
    print(f"file_quit: invoked={r['invoked']} after={r['after']}")

    # ventana sensible: kcalc pasa por sensible
    real = server.SENSITIVE
    server.SENSITIVE = (*real, "kcalc")
    try:
        assert next(w for w in (await call("desktop_state"))["windows"] if w["uuid"] == kcalc["uuid"])["sensitive"]
        for tool, args in [("app_actions", {"window": K}),
                           ("app_invoke", {"window": K, "action": "show_history"}),
                           ("window_set", {"window": K, "active": True})]:
            assert (await call(tool, **args))["error"]["type"] == "Refused", tool
        r = await call("screen_capture", target="screen")
        assert r["error"]["type"] == "SensitiveWindowOpen" and r["width"] == 0, r
        assert (await server.mcp.call_tool("screen_capture", {"target": "screen"})).content[0].type == "text"
    finally:
        server.SENSITIVE = real

    # Busy: con el candado cogido, espera LOCK_WAIT y falla
    wait, bs.LOCK_WAIT = bs.LOCK_WAIT, 0.2
    async with server._desk_lock:
        r = await call("window_set", window=K, active=True)
    bs.LOCK_WAIT = wait
    assert r["error"]["type"] == "Busy", r

    # auditoría
    audit = Path(n.env["XDG_DATA_HOME"]) / "manos" / "audit.jsonl"
    text = audit.read_text()
    lines = [json.loads(l) for l in text.splitlines()]
    assert {"desktop_state", "app_actions", "app_invoke", "window_set", "app_launch", "screen_capture"} <= \
        {l["tool"] for l in lines}, lines
    assert all(set(l) == {"ts", "pid", "tool", "rung", "via", "ok", "error_type", "resource_class", "action", "acting"}
               for l in lines)
    assert all(l["acting"] == (l["tool"] not in server.READS) for l in lines), lines
    assert {"Busy", "Refused", "SensitiveWindowOpen", "NeedsApproval"} <= {l["error_type"] for l in lines}
    assert any(l["action"] == "show_history" and l["via"] == "kxmlgui" and l["ok"] for l in lines), lines
    for w in kwin.state()["windows"]:
        assert w["caption"] not in text, w["caption"]
    print(f"auditoría: {len(lines)} líneas sin títulos")


with nested(1280, 800, "es") as n:
    assert kwin.BUS != "--user", kwin.BUS
    try:
        asyncio.run(main(n))
    finally:
        for pid in launched:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)

assert kwin.BUS == "--user"
assert (LIVE_AUDIT.exists() and LIVE_AUDIT.stat().st_size) == LIVE_AUDIT0, "escribió en la auditoría en vivo"
time.sleep(1)  # systemd-coredump tarda un momento en registrar
assert CORES().count("kwin_wayland") == CORES0 and f" {n.pid} " not in CORES(), "KWin anidada dejó un coredump"
print("test_apps_nest OK")
