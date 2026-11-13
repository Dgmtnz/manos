"""AT-SPI (desktop/atspi.py y su trabajador):  uv run --project . tests/test_atspi_nest.py

Puro: el selector de lanzadores ajenos sobre un /proc falso perdona el del servicio, el de una anidada de
test y el registryd activado por D-Bus; importar desktop.atspi no carga libatspi.

En KWin anidada: N12 IsEnabled perezoso y si una app Qt ya abierta entra después; N10 kcalc 7+3= con
`press` y un ref viejo da StaleHandle; `type` (EIS) y `set_text` contra `kdialog --inputbox`, con ida y
vuelta por su stdout; N11 registryd muerto → A11yDown, y un bus a11y falso → el trabajador muere y quien
llama sigue vivo.

Servidor por el protocolo (`mcp.Client` en memoria): ui_snapshot/ui_act y sus errores (sensible, StaleHandle,
Untypeable, ChordDenied); cancelar un `type` largo a medias corta en el bloque en curso, no deja teclas abajo,
suelta el candado y la llamada siguiente funciona; las filas de `--diag` (sin EIS en el plano), `--live-eis`,
`--live-input` y `--cure-a11y`, con y sin terminal.

En la anidada el bus a11y es dbus-broker, que activa registryd por systemd, y ahí no hay systemd: se
arranca a mano dentro de un scope de la anidada.
"""

import asyncio
import contextlib
import io
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anyio
import dbus
from mcp import Client

import kwin
import refs
import server
from desktop import atspi, eis
from nest import kwin_pid, nested

assert "gi" not in sys.modules and "gi.repository.Atspi" not in sys.modules, "importar atspi cargó gi"

# --- puro: el selector de la cura -----------------------------------------------------------------------
U = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service"
KEEP = f"{U}/app.slice/at-spi-dbus-bus.service"
with tempfile.TemporaryDirectory() as d:
    def proc(pid, comm, exe, cg):
        p = Path(d, str(pid))
        p.mkdir()
        (p / "comm").write_text(comm + "\n")
        (p / "cgroup").write_text(f"0::{cg}\n")
        os.symlink(exe, p / "exe")
    proc(10, "at-spi-bus-laun", atspi.LAUNCHER, KEEP)  # el del servicio
    proc(11, "at-spi-bus-laun", atspi.LAUNCHER, f"{U}/app.slice/app-app.liten.Gram-4242.scope")  # el ajeno
    proc(12, "at-spi-bus-laun", atspi.LAUNCHER, f"{U}/app.slice/manos-nest-77-abcdef-bus.scope")  # un test
    proc(13, "at-spi2-registr", "/usr/lib/at-spi2-registryd",  # el registryd sano, activado por D-Bus
         f"{U}/app.slice/app-dbus\\x2d:1.2\\x2dorg.a11y.atspi.Registry@0.service")
    proc(14, "at-spi-bus-laun", "/tmp/otro", f"{U}/app.slice/otro.scope")  # se llama igual, pero no lo es
    Path(d, "self").mkdir()
    assert atspi.rogue_launchers(d, KEEP) == [11], atspi.rogue_launchers(d, KEEP)
    assert [pid for pid, _, _ in atspi._pids("at-spi2-registr", d)] == [13]

# --- anidado -------------------------------------------------------------------------------------------
ROW = re.compile(r'\[(\d+)\] ([^"]+) ("(?:[^"\\]|\\.)*")(.*)')


def parse(snap):
    """(ref, rol, nombre, resto de la fila) de cada fila del listado."""
    return [(int(m[1]), m[2], json.loads(m[3]), m[4]) for m in map(ROW.fullmatch, snap["listing"])]


def ref(snap, role, name):
    return next(i for i, r, nm, _ in parse(snap) if (r, nm) == (role, name))


def window(p):
    deadline = time.monotonic() + 20
    while not (w := [w for w in kwin.state()["windows"] if w["pid"] == p.pid]):
        assert time.monotonic() < deadline, "la ventana no apareció"
        time.sleep(0.1)
    return w[0]["uuid"]


def until(uuid, ok=lambda s: True, timeout=6.0):
    """Relee la ventana hasta que `ok`: las acciones de Qt son asíncronas y las apps tardan en entrar."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            s = atspi.snapshot(uuid)
            if ok(s):
                return s
        except (atspi.NotInA11y, atspi.NoToplevel):
            s = None
        assert time.monotonic() < deadline, s and s["listing"]
        time.sleep(0.2)


def nest_pids(n, comm):
    """Pids vivos con ese comm DENTRO de los scopes de esta anidada; nada fuera de ella. Sin zombis: lo lanzado
    con n.launch es hijo nuestro (systemd-run --scope hace exec) hasta que alguien lo recoge."""
    out = []
    for p in Path("/proc").iterdir():
        try:
            if p.name.isdigit() and (p / "comm").read_text().strip() == comm and n.name in (p / "cgroup").read_text() \
                    and (p / "stat").read_text().rpartition(")")[2].split()[0] != "Z":
                out.append(int(p.name))
        except OSError:
            pass
    return out


def dialog(n):
    out = n.root / f"d{len(n.units)}.out"
    p = n.launch(["sh", "-c", f"exec kdialog --inputbox x > {out}"])
    return p, out, window(p)


CORES = lambda: subprocess.run(["coredumpctl", "list", "--no-legend"], capture_output=True, text=True).stdout
CORES0, PY0, LIVE = CORES().count("kwin_wayland"), CORES().count("python3"), kwin_pid()


async def server_checks(n, k):
    """Las tools ui_* por el protocolo, la cancelación, y la lógica de --diag y --cure-a11y."""
    async with Client(server.mcp) as c:
        async def call(tool, **args):
            return (await c.call_tool(tool, args)).structured_content

        s = await call("ui_snapshot", window=k, find="seven")
        assert s["ok"] and s["shown"] == 1 and s["generation"] == atspi._gen, s
        r = await call("ui_act", ref=ref(s, "button", "Seven"), action="press")
        assert r["ok"] and r["via"] == "atspi" and r["after"]["role"] == "button", r
        r = await call("ui_act", ref=10 ** 6, action="press")
        assert r["error"]["type"] == "StaleHandle" and "ui_snapshot" in r["error"]["next"], r
        r = await call("ui_snapshot", window="org.kde.nada")
        assert r["error"]["type"] == "NotFound" and "org.kde.kcalc" in r["error"]["available"], r

        p, out, u = dialog(n)
        until(u, lambda s: any(r == "text" for _, r, _, _ in parse(s)))
        s = await call("ui_snapshot", window=u)
        entry = next(i for i, r, _, _ in parse(s) if r == "text")
        for value, action, err in [("中", "type", "Untypeable"), ("meta+e", "key", "ChordDenied")]:
            r = await call("ui_act", ref=entry, action=action, value=value)
            assert r["error"]["type"] == err and r["error"]["next"], r

        real = server.SENSITIVE  # kcalc pasa por sensible: no se lee, no se toca y no se teclea con ella a la vista
        server.SENSITIVE = (*real, "kcalc")
        try:
            assert (await call("ui_snapshot", window=k))["error"]["type"] == "Refused"
            r = await call("ui_act", ref=entry, action="type", value="x")
            assert r["error"]["type"] == "SensitiveWindowOpen", r
        finally:
            server.SENSITIVE = real

        # Cancelación a mitad de un `type` largo: el bloque en curso acaba, nada más; el candado espera al hilo
        LONG = "abcdefgh" * 10
        t = time.monotonic() + 1.5
        with anyio.move_on_after(1.5):
            await c.call_tool("ui_act", {"ref": entry, "action": "type", "value": LONG})
            raise AssertionError("el type acabó antes de cancelarlo")
        back = time.monotonic() - t  # el cliente vuelve en cuanto abandona; el servidor sigue con su bloque
        while server._desk_lock.locked():
            assert time.monotonic() - t < 3, "el candado no se soltó"
            await asyncio.sleep(0.01)
        lag = time.monotonic() - t
        assert eis.ABORT.is_set()
        r = await call("ui_act", ref=entry, action="key", value="return")  # ModifierHeld si quedó algo abajo
        assert r["ok"] and r["via"] == "eis" and not eis.ABORT.is_set(), r
        p.wait(timeout=10)
        got = out.read_text()[:-1]
        assert out.read_text().endswith("\n") and LONG.startswith(got) and 0 < len(got) < len(LONG), got
        assert len(got) % eis.CHUNK == 0, got
        print(f"cancelación: llegaron {len(got)} de {len(LONG)} caracteres; el cliente volvió a los "
              f"{back * 1000:.0f} ms y el candado se soltó a los {lag * 1000:.0f} ms de cancelar")
        audit = [json.loads(l) for l in (Path(n.env["XDG_DATA_HOME"]) / "manos/audit.jsonl").read_text().splitlines()]
        assert any(l["tool"] == "ui_act" and l["error_type"] == "Cancelled" and l["via"] == "eis" and l["rung"] == "3"
                   for l in audit), audit[-3:]

    # --diag: las filas de escritorio, sin EIS
    dolphin = n.launch(["dolphin"])
    window(dolphin)
    connect = eis._connect
    eis._connect = lambda: (_ for _ in ()).throw(AssertionError("--diag plano abrió EIS"))
    try:
        rows = server._diag_desktop()
    finally:
        eis._connect = connect
    print("--diag:", *(f"\n  {r}" for r in rows))
    assert [ok for _, ok, _ in rows] == [True] * 4 and "acciones en /dolphin/Dolphin_1" in rows[3][2], rows
    ask, cure = server._confirm, atspi.cure
    try:
        server._confirm = lambda q: None  # sin terminal: SALTA, nunca PASS; la cura ni se intenta
        atspi.cure = lambda: (_ for _ in ()).throw(AssertionError("cura sin confirmar"))
        assert server._diag_live_eis()[1] is None and server._diag_live_input()[1] is None
        with contextlib.redirect_stdout(io.StringIO()) as o:
            assert server.cure_a11y() == 1
        assert "No se toca nada" in o.getvalue(), o.getvalue()
        server._confirm, atspi.cure = (lambda q: True), cure
        for row in (server._diag_live_eis(), server._diag_live_input()):
            print("--diag --live:", row)
            assert row[1] is True, row
        with contextlib.redirect_stdout(io.StringIO()) as o:
            assert server.cure_a11y() == 0
        assert "anidada" in o.getvalue(), o.getvalue()  # la cura nunca toca la sesión en vivo desde una anidada
    finally:
        server._confirm, atspi.cure = ask, cure

with nested(1280, 800, "es") as n:
    assert kwin.BUS != "--user", kwin.BUS
    # N12: una kcalc abierta ANTES de IsEnabled y sin QT_LINUX_ACCESSIBILITY_ALWAYS_ON
    early = window(n.launch(["kcalc"]))
    assert atspi.healthy() is None  # activa el lanzador; registryd no tiene quien lo active
    n.launch(["/usr/lib/at-spi2-registryd"])
    deadline = time.monotonic() + 10
    while not atspi.healthy():
        assert time.monotonic() < deadline, "registryd no contesta"
        time.sleep(0.1)
    fresh, enable = [], atspi.enable_once
    atspi.enable_once = lambda: fresh.append(enable()) or fresh[-1]
    t = time.monotonic()
    try:
        snap, k = atspi.snapshot(early), early
    except (atspi.NotInA11y, atspi.NoToplevel):
        snap = None
    print(f"N12: IsEnabled puesto; la kcalc ya abierta {'ENTRA' if snap else 'NO entra'} "
          f"({time.monotonic() - t:.1f} s)")
    if snap is None:
        k = window(n.launch(["kcalc"]))
        snap = until(k)
    bus = dbus.bus.BusConnection(n.address)
    assert atspi._status(bus, "IsEnabled")
    bus.close()
    assert fresh[0] is True and not any(fresh[1:]), fresh
    assert snap["generation"] == atspi._gen and snap["shown"] == len(snap["listing"]) <= snap["nodes"], snap

    # N10: kcalc 7+3= por AT-SPI. La vista previa del resultado nombra el panel; tras `=` el campo
    # (editable, así que solo su longitud) pasa de "7+3" a "10".
    seven = ref(snap, "button", "Seven")
    for b in ("Seven", "Add", "Three"):
        r = atspi.act(ref(snap, "button", b), "press")
        assert r["via"] == "atspi" and r["after"]["role"] == "button" and r["sent"] is None, r
    is_text = lambda s, v: any(r == "text" and rest.startswith(f" len={v}") for _, r, _, rest in parse(s))
    snap = until(k, lambda s: any(r == "panel" and nm == "10" for _, r, nm, _ in parse(s)) and is_text(s, 3))
    atspi.act(ref(snap, "button", "Equals"), "press")
    snap = until(k, lambda s: is_text(s, 2))
    print("N10:", [line for line in snap["listing"] if line.split("] ")[1].startswith(("text", "panel"))])
    try:
        atspi.act(seven, "press")
        raise AssertionError("un ref viejo actuó")
    except refs.StaleHandle:
        pass
    big = atspi.snapshot(k, max_nodes=5, coords=True)
    assert big["shown"] == 5 < big["nodes"] and all(re.search(r" @-?\d+,-?\d+,\d+,\d+$", x) for x in big["listing"]), big
    assert atspi.snapshot(k, find="equals")["listing"][0].endswith('button "Equals" (press)'), "find"
    try:
        atspi.act(ref(atspi.snapshot(k), "button", "Seven"), "fly")
        raise AssertionError("acción inventada")
    except atspi.NoSuchAction as e:
        assert "press" in str(e), e

    # `type` por EIS al campo de un kdialog, y `key return`: lo que sale por su stdout es lo que llegó
    TEXT = "Ñandú 7+3 ¿sí?"
    p, out, u = dialog(n)
    s = until(u, lambda s: any(r == "text" for _, r, _, _ in parse(s)))
    entry = next(i for i, r, _, _ in parse(s) if r == "text")
    calls, call = [], atspi._call  # cuánto tarda el campo en decir `focused` antes de abrir EIS
    def timed(req, acting=False):
        t = time.monotonic()
        out = call(req, acting)
        calls.append((req.get("action"), time.monotonic() - t, "focused" in ((out.get("after") or {}).get("states") or ())))
        return out
    atspi._call = timed
    try:
        r = atspi.act(entry, "type", TEXT)
    finally:
        atspi._call = call
    poll = calls[:next(i for i, c in enumerate(calls) if c[2]) + 1]
    print(f"foco: {len(poll)} lectura(s) hasta `focused`, {sum(c[1] for c in poll) * 1000:.0f} ms "
          f"({', '.join(f'{a} {d * 1000:.0f}' for a, d, _ in poll)})")
    assert r["via"] == "eis" and r["sent"] == len(TEXT) and "focused" in r["after"]["states"], r
    s = atspi.snapshot(u)
    assert s["secrets"] >= 1 and any(r == "text" and rest.startswith(f" len={len(TEXT)}")
                                     for _, r, _, rest in parse(s)), s["listing"]
    atspi.act(next(i for i, r, _, _ in parse(s) if r == "text"), "key", "return")
    p.wait(timeout=10)
    assert out.read_text() == TEXT + "\n", out.read_text()

    # `set_text` con relectura, y el botón OK por `press`
    p, out, u = dialog(n)
    s = until(u, lambda s: any(r == "text" for _, r, _, _ in parse(s)))
    r = atspi.act(next(i for i, r, _, _ in parse(s) if r == "text"), "set_text", "hola, mundo")
    assert r["verified"] is True and r["via"] == "atspi", r
    atspi.act(ref(s, "button", "OK"), "press")
    p.wait(timeout=10)
    assert out.read_text() == "hola, mundo\n", out.read_text()

    asyncio.run(server_checks(n, k))

    # N11: registryd de ESTA anidada muerto por su pid → A11yDown, refs fuera, y el proceso sigue
    regs = nest_pids(n, "at-spi2-registr")
    assert len(regs) == 1, regs
    old = ref(atspi.snapshot(k), "button", "Seven")
    gen = atspi._gen
    os.kill(regs[0], signal.SIGKILL)
    try:
        atspi.snapshot(k)
        raise AssertionError("sin registro y sin A11yDown")
    except atspi.A11yDown:
        pass
    assert atspi._gen > gen and atspi._proc is None
    try:
        atspi.act(old, "press")
        raise AssertionError("un ref de antes de la caída actuó")
    except refs.StaleHandle:
        pass
    os.environ["AT_SPI_BUS_ADDRESS"] = f"unix:path={n.root}/nobus"  # bus falso: ni se arranca el trabajador
    try:
        atspi.snapshot(k)
        raise AssertionError("bus falso y sin A11yDown")
    except atspi.A11yDown:
        assert atspi._proc is None
    finally:
        del os.environ["AT_SPI_BUS_ADDRESS"]
    healthy = atspi.healthy  # la salud miente: el trabajador arranca contra el bus falso y muere
    atspi.healthy = lambda timeout=2.0: f"unix:path={n.root}/nobus"
    # El trabajador aborta a propósito. RLIMIT_CORE=0 no basta: systemd-coredump apunta igual el aborto (sin
    # fichero) en coredumpctl; con 1 el kernel ni llama al recolector (medido). Lo heredan sus hijos.
    core = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (1, core[1]))
    t = time.monotonic()
    try:
        atspi.snapshot(k)
        raise AssertionError("trabajador sin bus y sin A11yDown")
    except atspi.A11yDown as e:
        print(f"N11: trabajador contra un bus falso: {str(e).split('(')[1].split(')')[0]}, "
              f"{time.monotonic() - t:.1f} s")
    finally:
        atspi.healthy = healthy
        resource.setrlimit(resource.RLIMIT_CORE, core)
    assert atspi._proc is None

    # Seguridad de la cura: con IsEnabled puesto, matar (SIGTERM, como la cura) el lanzador a11y de ESTA anidada,
    # elegido por pid dentro de sus scopes. La KWin anidada tiene que sobrevivir.
    n.launch(["/usr/lib/at-spi2-registryd"])  # N11 mató el anterior
    deadline = time.monotonic() + 10
    while not (addr := atspi.healthy()):
        assert time.monotonic() < deadline, "registryd no contesta"
        time.sleep(0.1)
    a11y = dbus.bus.BusConnection(addr)
    on_bus = set()
    for name in a11y.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                   "ListNames", "", ()):
        with contextlib.suppress(dbus.DBusException):
            on_bus.add(int(a11y.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                              "GetConnectionUnixProcessID", "s", (name,))))
    a11y.close()
    launchers, regs = nest_pids(n, "at-spi-bus-laun"), nest_pids(n, "at-spi2-registr")
    assert len(launchers) == 1 and len(regs) == 1, (launchers, regs)
    os.kill(launchers[0], signal.SIGTERM)
    time.sleep(2)
    alive = Path(f"/proc/{n.pid}").exists() and kwin_pid(f"--address={n.address}") == n.pid
    print(f"cura: KWin anidada {'conectada' if n.pid in on_bus else 'NO conectada'} al bus a11y; lanzador muerto "
          f"→ KWin {'VIVA' if alive else 'MUERTA'}, registryd {'vivo' if nest_pids(n, 'at-spi2-registr') else 'muerto'}, "
          f"bus a11y {'contesta' if atspi.healthy() else 'no contesta'}")
    assert alive and kwin.state()["outputs"], "la KWin anidada no sobrevivió a perder el lanzador a11y"

assert kwin.BUS == "--user" and kwin_pid() == LIVE
assert "gi.repository.Atspi" not in sys.modules
time.sleep(1)  # systemd-coredump tarda un momento en registrar
assert CORES().count("kwin_wayland") == CORES0, "KWin anidada dejó un coredump"
assert CORES().count("python3") == PY0, "un python3 dejó un coredump"
print("test_atspi_nest OK")
