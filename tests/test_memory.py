"""Memoria por app (memory.py), por el protocolo y en KWin anidada con kwrite:  uv run --project . tests/test_memory.py

Tarea 1: app_launch sobre un fichero con un nombre trazador (sale en el título), set_text con un valor trazador, una
acción que no existe y file_save_all. Un submit rechazado no escribe nada; el aceptado escribe
`memory/org.kde.kwrite.md` (0700/0600) con los recuentos, el sondeo (versión, KXmlGui, AT-SPI) y ni el valor, ni el
título, ni la acción inventada. Se sirve como `skill://memory/<clase>` y sale en `skill://index`. Tarea 2: app_launch
y desktop_state lo nombran en `next`, y el segundo gate suma a los recuentos sin volver a sondear.
"""

import asyncio
import contextlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client

import kwin
import memory
import server
from desktop import atspi
from nest import nested

CORES = lambda: subprocess.run(["coredumpctl", "list", "--no-legend"], capture_output=True, text=True).stdout
CORES0 = CORES().count("kwin_wayland")
ROW = re.compile(r'\[(\d+)\] ([^"]+) ("(?:[^"\\]|\\.)*")(.*)')
VALUE, TITLE, KW = "VALOR-QJW5521", "TITULOZQX81", "org.kde.kwrite"
launched = []

# Puro: nombres que no son un resourceClass no tienen memoria; la pista solo sale con fichero
assert memory.path("") is memory.path("../x") is memory.path(".oculto") is memory.path("a/b") is None
assert memory.hint(["org.kde.nada", None]) is None
# Puro: una clase sensible con una acción acertada (un app_launch, p. ej.) ni se sondea ni se recuerda
with tempfile.TemporaryDirectory() as d:
    saved, os.environ["XDG_DATA_HOME"] = os.environ.get("XDG_DATA_HOME"), d
    real_probe, memory.probe = memory.probe, lambda app: (_ for _ in ()).throw(AssertionError(f"sondeó {app}"))
    try:
        (Path(d) / "manos").mkdir()
        row = {"ts": 1.0, "pid": os.getpid(), "acting": True, "tool": "app_launch", "rung": "1",
               "resource_class": "org.kde.kwalletmanager5", "action": "org.kde.kwalletmanager5", "ok": True,
               "error_type": None}
        # la fila de otro servidor (otra sesión, o la misma tarea reanudada) no cuenta para esta
        (Path(d) / "manos/audit.jsonl").write_text(json.dumps({**row, "pid": -1, "resource_class": "org.kde.dolphin"})
                                                   + "\n" + json.dumps(row) + "\n")
        assert memory.learn(0.0, server.sensitive) == [] and not (Path(d) / "manos/memory").exists()
        memory.probe = lambda app: None  # lo que la paró fue el filtro: sin él, sí se escribe (bajo el cerrojo)
        assert memory.learn(0.0, lambda app: False) == ["org.kde.kwalletmanager5"]
        mem0 = Path(d) / "manos/memory"
        assert (mem0 / "org.kde.kwalletmanager5.md").is_file() and (mem0 / ".lock").exists()
    finally:
        memory.probe = real_probe
        os.environ.pop("XDG_DATA_HOME")
        if saved is not None:
            os.environ["XDG_DATA_HOME"] = saved


async def main(n):
    mem = Path(n.env["XDG_DATA_HOME"]) / "manos/memory"
    probes, real_probe = [], memory.probe
    memory.probe = lambda app: probes.append(app) or real_probe(app)
    async with Client(server.mcp) as c:
        sc = lambda r: r.structured_content

        async def call(tool, **args):
            return sc(await c.call_tool(tool, args))

        async def editor(win):
            deadline = time.monotonic() + 10
            while not (snap := await call("ui_snapshot", window=win))["ok"]:  # kwrite tarda en entrar en AT-SPI
                assert time.monotonic() < deadline and snap["error"]["type"] in ("NoToplevel", "NotInA11y"), snap
                await asyncio.sleep(0.3)
            hit = [int(m[1]) for m in map(ROW.fullmatch, snap["listing"]) if m and m[2] == "text" and "len=" in m[4]]
            assert hit, snap["listing"]
            return hit[0], snap

        async def submit(task, oid):
            r = await c.call_tool("submit_task_complete", {"task": task, "evidence": {"observation_id": oid}})
            return r.is_error, sc(r)

        templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
        assert "skill://memory/{app}" in templates, templates
        assert "Memoria de apps" not in (await c.read_resource("skill://index")).contents[0].text
        try:
            await c.read_resource(f"skill://memory/{KW}")
        except Exception as exc:
            assert "No hay memoria" in str(exc), exc
        else:
            raise AssertionError("memoria servida sin fichero")

        # tarea 1
        f1 = n.root / f"nota-{TITLE}.txt"
        f1.write_text("x\n")
        A = lambda f: [{"kind": "file", "check": "modified_after_start", "path": str(f)},
                       {"kind": "file", "check": "regex", "path": str(f), "value": "QJW"}]
        assert (await call("task_checkpoint", task="mem-uno", plan=["escribir", "guardar"], accept=A(f1)))["ok"]
        r = await call("app_launch", app=KW, url=str(f1))
        assert r["ok"] and "next" not in r, r
        launched.append(r["pid"])
        w1 = r["window"]
        deadline = time.monotonic() + 10
        while TITLE not in next(w["caption"] for w in kwin.state()["windows"] if w["uuid"] == w1):
            assert time.monotonic() < deadline, "el título no lleva el nombre del fichero"
            await asyncio.sleep(0.2)
        ed, _ = await editor(w1)
        assert (await call("ui_act", ref=ed, action="set_text", value=VALUE))["ok"]
        assert (await call("app_invoke", window=w1, action="no_existe_x"))["error"]["type"] == "NoSuchAction"
        err, v = await submit("mem-uno", (await call("desktop_state"))["observation_id"])
        assert err and v["error"]["type"] == "NotDone", v
        assert not mem.exists(), "un gate rechazado escribió memoria"
        assert (await call("app_invoke", window=w1, action="file_save_all"))["ok"]
        deadline = time.monotonic() + 5
        while VALUE not in f1.read_text():
            assert time.monotonic() < deadline, "kwrite no guardó"
            await asyncio.sleep(0.1)
        err, v = await submit("mem-uno", (await call("ui_snapshot", window=w1))["observation_id"])
        assert not err and v["status"] == "done", v

        p = mem / f"{KW}.md"
        text = p.read_text()
        print(text)
        assert stat.S_IMODE(mem.stat().st_mode) == 0o700 and stat.S_IMODE(p.stat().st_mode) == 0o600
        assert len(text.splitlines()) <= 40, len(text.splitlines())
        for want in ("- peldaño 1 · app_launch org.kde.kwrite: 1", "- peldaño 2 · ui_act set_text: 1",
                     "- peldaño 1 · app_invoke file_save_all: 1", "- peldaño 1 · app_invoke → NoSuchAction: 1",
                     "- Tareas certificadas con esta app: 1.", f"- Versión: {memory._version(KW)}."):
            assert want in text, want
        assert memory._version(KW) and re.search(r"KXmlGui: \d+ acciones", text) and \
            re.search(r"AT-SPI: \d+ elementos, \d+ con acción", text), text
        captions = [w["caption"] for w in kwin.state()["windows"] if w["caption"]]
        for secret in (VALUE, "QJW5521", TITLE, "no_existe_x", *captions):
            assert secret not in text, secret
        assert probes == [KW], probes

        assert (await c.read_resource(f"skill://memory/{KW}")).contents[0].text == text
        assert f"skill://memory/{KW}" in (await c.read_resource("skill://index")).contents[0].text

        # tarea 2: la pista y la suma
        f2 = n.root / "otra.txt"
        f2.write_text("y\n")
        assert (await call("task_checkpoint", task="mem-dos", plan=["escribir", "guardar"], accept=A(f2)))["ok"]
        r = await call("app_launch", app=KW, url=str(f2))
        assert r["ok"] and f"skill://memory/{KW}" in r["next"], r
        launched.append(r["pid"])
        w2 = r["window"]
        st = await call("desktop_state")
        assert f"skill://memory/{KW}" in st["next"], st.get("next")
        ed, _ = await editor(w2)
        assert (await call("ui_act", ref=ed, action="set_text", value=VALUE))["ok"]
        assert (await call("app_invoke", window=w2, action="file_save_all"))["ok"]
        deadline = time.monotonic() + 5
        while VALUE not in f2.read_text():
            assert time.monotonic() < deadline, "kwrite no guardó"
            await asyncio.sleep(0.1)
        err, v = await submit("mem-dos", (await call("ui_snapshot", window=w2))["observation_id"])
        assert not err, v
        text = p.read_text()
        for want in ("- peldaño 1 · app_launch org.kde.kwrite: 2", "- peldaño 2 · ui_act set_text: 2",
                     "- peldaño 1 · app_invoke file_save_all: 2", "- peldaño 1 · app_invoke → NoSuchAction: 1",
                     "- Tareas certificadas con esta app: 2."):
            assert want in text, (want, text)
        assert probes == [KW], probes  # sondeado una vez
        assert VALUE not in text and TITLE not in text
    memory.probe = real_probe


with nested(1280, 800, "es") as n:
    assert kwin.BUS != "--user", kwin.BUS
    atspi.healthy()  # activa el lanzador; registryd se arranca a mano dentro de la anidada (no hay systemd)
    n.launch(["/usr/lib/at-spi2-registryd"])
    deadline = time.monotonic() + 10
    while not atspi.healthy():
        assert time.monotonic() < deadline, "registryd no contesta"
        time.sleep(0.1)
    try:
        asyncio.run(main(n))
    finally:
        for pid in launched:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)

assert kwin.BUS == "--user"
time.sleep(1)  # systemd-coredump tarda un momento en registrar
assert CORES().count("kwin_wayland") == CORES0 and f" {n.pid} " not in CORES(), "KWin anidada dejó un coredump"
print("test_memory OK")
