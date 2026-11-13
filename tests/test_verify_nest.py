"""P4 por el protocolo (`mcp.Client` en memoria):  uv run --project . tests/test_verify_nest.py

En KWin anidada, con kcalc: task_checkpoint → ui_act → observar → submit_task_complete, primero rechazado (isError,
con cada aserción fallida, su valor observado y su `next`) y luego aceptado, con la tarea marcada en su JSON. Ids
de observación de cada tool que observa; un id inventado, uno de antes de empezar, sin aserciones o con un tipo
que no existe: rechazo sin comprobar nada. Detector de bucles: tres Seven seguidos (el visor cambia) pasan; el
tercer Clear seguido y el tercer window_set igual son `Stuck` y no se hacen; dos app_invoke fallidos dan `hint`.
Después, en la sesión en vivo como los tests de P2: las aserciones de navegador contra el fixture `ground.html`.
"""

import asyncio
import json
import os
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client

import kwin
import server
from desktop import atspi
from nest import nested

CORES = lambda: subprocess.run(["coredumpctl", "list", "--no-legend"], capture_output=True, text=True).stdout
CORES0 = CORES().count("kwin_wayland")
LIVE_TASKS = Path.home() / ".local/share/manos/tasks"
LIVE_TASKS0 = sorted(LIVE_TASKS.glob("*")) if LIVE_TASKS.exists() else []
ROW = re.compile(r'\[(\d+)\] ([^"]+) ("(?:[^"\\]|\\.)*")(.*)')
NEW = {"task_checkpoint", "task_resume", "submit_task_complete"}
OBSERVERS = {"browser_observe", "form_describe", "ui_snapshot", "screen_capture", "desktop_state"}


def ref(snap, role, name):
    return next(int(m[1]) for m in map(ROW.fullmatch, snap["listing"]) if (m[2], json.loads(m[3])) == (role, name))


def window(p):
    deadline = time.monotonic() + 20
    while not (w := [w for w in kwin.state()["windows"] if w["pid"] == p.pid]):
        assert time.monotonic() < deadline, "la ventana no apareció"
        time.sleep(0.1)
    return w[0]["uuid"]


async def main(n, k):
    async with Client(server.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert NEW <= set(tools) and all(tools[t].output_schema for t in NEW | OBSERVERS), sorted(tools)
        assert all("observation_id" in tools[t].output_schema["properties"] for t in OBSERVERS)
        # las instrucciones caben en los 4096 caracteres de Claude Code; cada skills/*.md, resource skill://<nombre>
        assert c.instructions == server.INSTRUCTIONS and len(c.instructions) <= 3900, len(c.instructions)
        assert "`desktop_state` primero, siempre." in c.instructions and "skill://method" in c.instructions
        # `acting: false` en la auditoría: exactamente las de readOnlyHint
        assert server.READS == {t for t in tools if tools[t].annotations.read_only_hint}, server.READS
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert {"skill://method", "skill://forms", "skill://index"} <= uris, uris
        forms_md = (await c.read_resource("skill://forms")).contents[0].text
        assert forms_md == (Path(server.HERE) / "skills/forms.md").read_text(), forms_md[:200]

        async def call(tool, **args):
            r = await c.call_tool(tool, args)
            if tool == "submit_task_complete":  # el texto que lee un cliente sin structuredContent dice lo mismo
                assert json.loads(r.content[0].text) == r.structured_content
            return r

        sc = lambda r: r.structured_content
        early = sc(await call("desktop_state", include=["windows"]))["observation_id"]
        assert re.fullmatch(r"obs-[0-9a-f]{12}", early), early
        report = n.root / "informe.txt"
        A = [{"kind": "file", "check": "exists", "path": str(report)},
             {"kind": "file", "check": "regex", "path": str(report), "value": r"Resultado: 777\b"},
             {"kind": "file", "check": "modified_after_start", "path": str(report)},
             {"kind": "window", "check": "exists", "resource_class": "org.kde.kcalc"}]
        t = sc(await call("task_checkpoint", task="nest-kcalc", plan=["teclear 777", "guardar informe", "comprobar"],
                          note="kcalc en la anidada", accept=A))  # los criterios, al empezar
        assert t["ok"] and t["status"] == "open" and t["started"] > 0 and t["accept"] == A, t
        r = sc(await call("task_checkpoint", task="../fuera", plan=["x"]))
        assert r["error"]["type"] == "ValueError", r
        r = sc(await call("task_checkpoint", task="nest-kcalc", accept=A[1:]))
        assert r["error"]["type"] == "BadEvidence" and "solo puede crecer" in r["error"]["message"], r
        assert sc(await call("task_checkpoint", task="sin-criterios", plan=["x"]))["ok"]
        fresh = sc(await call("desktop_state", include=["windows"]))["observation_id"]

        async def submit(oid, asserts=(), task="nest-kcalc"):
            return await call("submit_task_complete", task=task,
                              evidence={"observation_id": oid, "assertions": list(asserts)})

        # rechazos sin comprobar nada
        for oid, asserts, task, want in [
                (early, (), "nest-kcalc", "antes de empezar"),
                ("obs-0123456789ab", (), "nest-kcalc", "olvidado"),
                (fresh, (), "sin-criterios", "task_checkpoint(accept="),
                (fresh, [{"kind": "file", "check": "regex", "path": str(report), "value": ".*"}], "nest-kcalc",
                 "no puede fallar"),
                (fresh, [{"kind": "window", "check": "exists"}], "nest-kcalc", "uuid` o `resource_class"),
                (fresh, [{"kind": "process", "check": "exit_code", "cmd": "true"}], "nest-kcalc", "código de salida"),
                (early, (), "no-abierta", None)]:
            r = await submit(oid, asserts, task)
            v = sc(r)
            assert r.is_error and not v["ok"] and v["passed"] == 0 and v["error"]["next"], v
            assert (want in v["error"]["message"]) if want else v["error"]["type"] == "NoSuchTask", v

        # actuar: Seven tres veces seguidas; el visor cambia, así que ninguna es Stuck. La huella AX se mide aquí.
        digests, real_digest = [], atspi.digest

        def timed(u):
            t0 = time.perf_counter()
            try:
                return real_digest(u)
            finally:
                digests.append((time.perf_counter() - t0) * 1000)
        atspi.digest = timed
        acts, real_act = [], atspi.act
        atspi.act = lambda *a: acts.append(a) or real_act(*a)
        try:
            snap = sc(await call("ui_snapshot", window=k))
            seven = ref(snap, "button", "Seven")
            for _ in range(3):
                r = sc(await call("ui_act", ref=seven, action="press"))
                assert r["ok"] and "hint" not in r, r
                await asyncio.sleep(0.5)  # Qt actualiza el visor de forma asíncrona; un agente tarda más que esto
            assert len(acts) == 3 and len(digests) == 2, (acts, digests)

            # rechazo: el informe aún no existe
            snap = sc(await call("ui_snapshot", window=k))
            r = await submit(snap["observation_id"])
            v = sc(r)
            assert r.is_error and v["error"]["type"] == "NotDone" and v["status"] == "open" and v["passed"] == 1, v
            assert [(f["kind"], f["check"]) for f in v["failed"]] == [("file", "exists"), ("file", "regex"),
                                                                     ("file", "modified_after_start")], v
            assert all(f["observed"] == "no existe" and f["target"] == str(report) and f["next"] for f in v["failed"]), v
            print("rechazo:", json.dumps(v["failed"][0], ensure_ascii=False))

            # Clear: la 1ª borra el 777, la 2ª no cambia nada, la 3ª es Stuck y no se hace; la 4ª vuelve a leer el
            # árbol (no hay guardia de _desk que la pare antes) y, como sigue igual, también es Stuck
            clear = ref(snap, "button", "Clear")
            acts.clear()
            got = []
            for _ in range(4):
                got.append(sc(await call("ui_act", ref=clear, action="press")))
                await asyncio.sleep(0.5)
            assert got[0]["ok"] and got[1]["ok"] and got[2]["error"]["type"] == got[3]["error"]["type"] == "Stuck", got
            assert "app_actions" in got[2]["error"]["message"] and got[2]["error"]["next"], got[2]
            assert len(acts) == 2 and len(digests) == 5, (acts, digests)  # Clear 2ª, 3ª y 4ª leyeron el árbol
        finally:
            atspi.digest, atspi.act = real_digest, real_act
        print(f"huella AX: {len(digests)} lecturas, mediana {statistics.median(digests):.0f} ms "
              f"(máx {max(digests):.0f}); solo cuando una ui_act repite la anterior")

        # window_set con la misma geometría: la 3ª no llega a KWin
        sets, real_set = [], server._window_set
        server._window_set = lambda *a: sets.append(a) or real_set(*a)
        try:
            got = [sc(await call("window_set", window=k, geometry=[40, 50, 520, 600])) for _ in range(3)]
        finally:
            server._window_set = real_set
        assert got[0]["ok"] and got[1]["ok"] and got[2]["error"]["type"] == "Stuck" and len(sets) == 2, got
        assert "desktop_state" in got[2]["error"]["message"], got[2]
        assert sc(await call("window_set", window=k, active=True))["ok"]  # otra acción: se suelta

        # FailureTracker: pista al 2º fallo seguido en la misma diana; el 3º idéntico ya es Stuck
        got = [sc(await call("app_invoke", window=k, action="no_existe_x")) for _ in range(3)]
        assert [g["error"]["type"] for g in got] == ["NoSuchAction", "NoSuchAction", "Stuck"], got
        assert "hint" not in got[0] and got[1]["hint"].startswith(f"2 fallos seguidos de app_invoke en {k}"), got[1]
        assert "ui_snapshot" in got[1]["hint"], got[1]

        # aceptación: el informe, una observación nueva y todo en verde
        report.write_text("Resultado: 777\n")
        cap = sc(await call("screen_capture", target=k, max_edge=320))
        assert cap["ok"] and cap["observation_id"].startswith("obs-"), cap
        r = await submit(cap["observation_id"], [{"kind": "window", "check": "title_regex", "value": "."}])
        v = sc(r)
        assert not r.is_error and v["ok"] and v["status"] == "done" and v["passed"] == 5 and not v["failed"], v
        stored = json.loads((Path(n.env["XDG_DATA_HOME"]) / "manos/tasks/nest-kcalc.json").read_text())
        assert stored["status"] == "done" and stored["evidence"]["assertions"] == 5, stored
        assert stat.S_IMODE((Path(n.env["XDG_DATA_HOME"]) / "manos/tasks/nest-kcalc.json").stat().st_mode) == 0o600
        r = sc(await call("task_resume", task="nest-kcalc"))
        assert r["ok"] and r["status"] == "done" and r["plan"][0] == "teclear 777", r
        r = await submit(cap["observation_id"])
        assert r.is_error and "ya está terminada" in sc(r)["error"]["message"], sc(r)
        print("aceptada:", {k_: v[k_] for k_ in ("status", "passed")})


async def browser():
    """Las aserciones de navegador, en la sesión en vivo como los tests de P2 (navegador aparcado)."""
    async with Client(server.mcp) as c:
        sc = lambda r: r.structured_content
        B = lambda check, value: {"kind": "browser", "check": check, "value": value}
        good = [B("selector_present", "#nombre"), B("selector_absent", "#no-existe"),
                B("text_regex", "Nombre completo"), B("url_regex", r"ground\.html$")]
        assert sc(await c.call_tool("task_checkpoint", {"task": "nav-ground", "plan": ["abrir el fixture"],
                                                        "accept": good}))["ok"]
        url = (Path(__file__).parent / "fixtures/ground.html").as_uri()
        try:
            o = sc(await c.call_tool("browser_open", {"url": url}))
            assert o["ok"], o
            ob = sc(await c.call_tool("browser_observe", {}))
            assert ob["ok"] and ob["observation_id"].startswith("obs-"), ob
            ev = lambda a: {"task": "nav-ground", "evidence": {"observation_id": ob["observation_id"], "assertions": a}}
            r = await c.call_tool("submit_task_complete", ev(good + [B("selector_present", "#nada")]))
            assert r.is_error and [f["target"] for f in sc(r)["failed"]] == ["#nada"], sc(r)
            r = await c.call_tool("submit_task_complete", ev(good))
            assert not r.is_error and sc(r)["status"] == "done", sc(r)
        finally:
            await c.call_tool("browser_close", {})
    # la auditoría del navegador: peldaño web, `acting` según la tool, y ni la url ni el título
    text = (Path(os.environ["XDG_DATA_HOME"]) / "manos/audit.jsonl").read_text()
    web = {l["tool"]: l for l in map(json.loads, text.splitlines()) if l["rung"] == "web"}
    assert set(web) == {"browser_open", "browser_observe", "browser_close"}, web
    assert all(set(l) == {"ts", "pid", "tool", "rung", "via", "ok", "error_type", "resource_class", "action", "acting"}
               and l["ok"] and l["acting"] == (t != "browser_observe") for t, l in web.items()), web
    assert "ground" not in text and "terreno" not in text, text  # ni la url ni el título del fixture
    print("auditoría web:", json.dumps(web["browser_open"]))


with nested(1280, 800, "es") as n:
    assert kwin.BUS != "--user", kwin.BUS
    atspi.healthy()  # activa el lanzador; registryd se arranca a mano dentro de la anidada (no hay systemd)
    n.launch(["/usr/lib/at-spi2-registryd"])
    deadline = time.monotonic() + 10
    while not atspi.healthy():
        assert time.monotonic() < deadline, "registryd no contesta"
        time.sleep(0.1)
    atspi.enable_once()
    k = window(n.launch(["kcalc"]))
    deadline = time.monotonic() + 10
    while True:  # que kcalc ya esté en AT-SPI
        try:
            atspi.snapshot(k)
            break
        except (atspi.NotInA11y, atspi.NoToplevel):
            assert time.monotonic() < deadline, "kcalc no entró en AT-SPI"
            time.sleep(0.3)
    asyncio.run(main(n, k))

assert kwin.BUS == "--user"
with tempfile.TemporaryDirectory() as d:  # la tarea del navegador no cae en ~/.local/share
    saved, os.environ["XDG_DATA_HOME"] = os.environ.get("XDG_DATA_HOME"), d
    try:
        asyncio.run(browser())
    finally:
        os.environ.pop("XDG_DATA_HOME")
        if saved is not None:
            os.environ["XDG_DATA_HOME"] = saved
assert (sorted(LIVE_TASKS.glob("*")) if LIVE_TASKS.exists() else []) == LIVE_TASKS0, "escribió tareas en vivo"
time.sleep(1)  # systemd-coredump tarda un momento en registrar
assert CORES().count("kwin_wayland") == CORES0 and f" {n.pid} " not in CORES(), "KWin anidada dejó un coredump"
print("test_verify_nest OK")
