"""El escritorio contra una KWin anidada:  uv run --project . tests/test_desktop_nest.py

N1: `kwin.state()` ve una salida de 1280x800 a escala 1 y las ventanas que se abren; `activate()` pasa
el foco de una a otra y `active()` lo sigue. `shell_pids()` da la KWin anidada, no la de verdad. Mide
lo que cuestan `active()` (corre antes de cada bloque de pulsaciones, presupuesto 60 ms) y `state()`.

N3-N6, teclado por EIS contra `kdialog --inputbox`, cuyo stdout es lo que de verdad llegó: N3 REPERTOIRE
ida y vuelta, también con CapsLock; una sola sesión a la vez; ModifierHeld con Shift abajo en otro
dispositivo; N4 un proceso muerto con Shift abajo no tumba KWin (guardián EIS) y la siguiente sesión
escribe `a`; N5 ABORT y el fichero de parada cortan en el bloque en curso; N6 un
robo de foco da FocusLost con ≤8 caracteres y ningún Intro en la otra ventana.

N7, captura contra una ventana GTK3 roja: el centro lee (255,0,0), el recorte mide el cliente, el Space
lleva el puntero al píxel (±1 px) y caduca con la captura siguiente; spectacle forzado da imagen válida;
`occluded_by` nombra lo que la tapa y una ventana minimizada da NotVisible. Mide las dos vías.
"""

import hashlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbus
from PIL import Image

import kwin
import spaces
from browser import session as browser
from desktop import capture, eis
from desktop.xkb_es import REPERTOIRE
from nest import kwin_pid, nested


def timed(fn, *args, n=10):
    t = time.monotonic()
    for _ in range(n):
        out = fn(*args)
    return out, (time.monotonic() - t) / n * 1000


def dialog(n):
    """Un `kdialog --inputbox` en la anidada → (uuid, Popen, fichero con su stdout)."""
    out = n.root / f"d{len(n.units)}.out"
    p = n.launch(["sh", "-c", f"exec kdialog --inputbox x > {out}"])
    deadline = time.monotonic() + 20
    while not (w := [w for w in kwin.state()["windows"] if w["pid"] == p.pid]):
        assert time.monotonic() < deadline, "kdialog no apareció"
        time.sleep(0.1)
    kwin.activate(w[0]["uuid"])
    return w[0]["uuid"], p, out


def submit(u, p, out, text="\n"):
    with eis.session(u) as kb:
        kb.type(text)
    p.wait(timeout=10)
    return out.read_text()


def on_stroke(kb, n, fn):
    """Llama a `fn` justo antes de la pulsación número `n` (1 = la primera)."""
    stroke, count = kb._stroke, [0]
    def wrapped(*a):
        count[0] += 1
        if count[0] == n:
            fn()
        stroke(*a)
    kb._stroke = wrapped


HELPER = """
import os, sys, time
sys.path.insert(0, %r)
import kwin
addr = os.environ["DBUS_SESSION_BUS_ADDRESS"]
assert "manos-nest-" in addr, addr
kwin.BUS = "--address=" + addr
from desktop import eis
with eis.session(None) as kb:
    kb._send(42, 1)
    kb.sync()
    print(kb.guard.pid, flush=True)
    time.sleep(60)
""" % str(Path(__file__).resolve().parent.parent)
CORES = lambda: subprocess.run(["coredumpctl", "list", "--no-legend"], capture_output=True, text=True).stdout
TEXT = "abcdefghijklmnop\n"  # dos bloques de 8 y el Intro solo en el tercero
LIVE = kwin_pid()
CORES0 = CORES().count("kwin_wayland")

with nested(1280, 800, "es") as n:
    assert kwin.BUS != "--user", kwin.BUS
    assert kwin.shell_pids() == {n.pid} and LIVE not in kwin.shell_pids(), (kwin.shell_pids(), n.pid, LIVE)

    st = kwin.state()
    assert st["outputs"] == [{"name": st["outputs"][0]["name"], "x": 0, "y": 0, "width": 1280, "height": 800,
                              "scale": 1}], st["outputs"]
    assert st["virtual_screen"] == [1280, 800] and st["work_area"] == [0, 0, 1280, 800], st
    assert st["windows"] == [] and st["active"] is None, st

    n.launch(["kcalc"])
    n.launch(["kcalc"])
    deadline = time.monotonic() + 20
    while len(kwin.windows("org.kde.kcalc")) < 2:
        assert time.monotonic() < deadline, "las dos kcalc no aparecieron"
        time.sleep(0.1)
    uuids = {w["uuid"] for w in kwin.windows("org.kde.kcalc")}

    st, ms_state = timed(kwin.state, n=5)
    wins = st["windows"]
    assert {w["uuid"] for w in wins} == uuids, (wins, uuids)
    for w in wins:
        assert w["resource_class"] == "org.kde.kcalc" and w["caption"] and w["pid"] > 0, w
        assert w["output"] == st["outputs"][0]["name"] and not w["minimized"], w
        assert len(w["frame"]) == 4 and len(w["client"]) == 4, w
    assert [w["active"] for w in wins].count(True) == 1 and st["active"] in uuids, st
    assert wins[-1]["uuid"] == st["active"], "la activa no está arriba del apilado"

    for u in [*uuids, *uuids]:
        kwin.activate(u)
        assert kwin.active() == u, (kwin.active(), u)
        assert kwin.state()["windows"][-1]["uuid"] == u, "activar no la subió"
    a, ms_active = timed(kwin.active, n=20)
    assert a == u, a
    try:
        kwin.activate("00000000-0000-0000-0000-000000000000")
        raise AssertionError("activó una ventana que no existe")
    except kwin.KWinError:
        pass
    print(f"active(): {ms_active:.1f} ms  state() con 2 ventanas: {ms_state:.1f} ms")

    # N3: REPERTOIRE ida y vuelta, el keymap de KWin planifica igual que el `es` offline
    u, p, out = dialog(n)
    lat, active = [], kwin.active
    def timed_active():
        t = time.monotonic()
        a = active()
        lat.append((time.monotonic() - t) * 1000)
        return a
    kwin.active = timed_active
    try:
        with eis.session(u) as kb:
            assert kb.keymap.plan(REPERTOIRE) == eis.xkb_es.Keymap.from_names().plan(REPERTOIRE)
            t = time.monotonic()
            assert kb.type(REPERTOIRE + "\n") == len(REPERTOIRE) + 1
    finally:
        kwin.active = active
    print(f"REPERTOIRE en {time.monotonic() - t:.1f} s; active() por bloque (ms):", " ".join(f"{x:.0f}" for x in lat))
    assert max(lat) < 60, lat
    p.wait(timeout=10)
    assert out.read_text() == REPERTOIRE + "\n", (out.read_text(), REPERTOIRE)
    u, p, out = dialog(n)  # con CapsLock bloqueado en OTRA sesión: la nueva tiene que enterarse al empezar
    with eis.session(u) as kb:
        kb.key("caps_lock")
    with eis.session(u) as kb:
        assert kb.lock_mods, "la sesión nueva no ve el CapsLock bloqueado"
        kb.type(REPERTOIRE)
        assert kb.lock_mods, "CapsLock no quedó bloqueado"
        kb.key("caps_lock")
        kb.key("return")
        kb.sync()
        assert kb.depressed == 0 and kb.lock_mods == 0, (kb.depressed, kb.lock_mods)
    p.wait(timeout=10)
    assert out.read_text() == REPERTOIRE + "\n", (out.read_text(), REPERTOIRE)
    with eis.session(None) as kb:  # el puntero, en lógicas; y una segunda sesión a la vez no arranca
        try:
            with eis.session(None):
                raise AssertionError("dos sesiones a la vez")
        except eis.InputUnavailable as e:
            assert "Otra sesión" in str(e), e
        kb.move(321, 123)
        kb.sync()
    assert kwin.state()["cursor"] == [321, 123], kwin.state()["cursor"]
    fd, link, g = kwin.eis_open()  # otro dispositivo con Shift abajo, como el teclado de Diego
    raw = eis._Session(None)
    raw._setup(fd)
    raw._send(42, 1)
    raw.sync()
    try:
        with eis.session(None):
            raise AssertionError("no saltó ModifierHeld")
    except eis.ModifierHeld:
        pass
    raw._send(42, 0)
    raw.sync()
    raw.close()
    kwin.eis_close(link, g, clean=True)

    # N4: quien pulsa muere (kill -9 y SIGTERM, por su pid) con Shift abajo y el socket EIS abierto. Sin el
    # guardián, KWin 6.8.2 se caía al siguiente evento de teclado (4 de 4); con él sigue viva, el guardián
    # desconecta (KWin suelta Shift), sale y libera el cerrojo, y la siguiente sesión escribe `a`, no `A`.
    for sig in (signal.SIGKILL, signal.SIGTERM):
        h = subprocess.Popen([sys.executable, "-c", HELPER], stdout=subprocess.PIPE, text=True)
        guard = Path(f"/proc/{int(h.stdout.readline())}")
        try:  # el cerrojo también vale entre procesos
            with eis.session(None):
                raise AssertionError("dos sesiones a la vez")
        except eis.InputUnavailable:
            pass
        os.kill(h.pid, sig)
        h.wait()
        deadline = time.monotonic() + 5
        while guard.exists():
            assert time.monotonic() < deadline, "el guardián no salió"
            time.sleep(0.05)
        u, p, out = dialog(n)
        with eis.session(u) as kb:
            kb.type("a\n")
        p.wait(timeout=10)
        assert out.read_text() == "a\n", out.read_text()
        assert kwin.shell_pids() == {n.pid}, (sig, kwin.shell_pids(), n.pid)

    # N5: ABORT y el fichero de parada a mitad del primer bloque: se termina ese bloque y nada más
    stop = n.root / "stop"
    for name, trigger in [("ABORT", eis.ABORT.set), ("parada", stop.touch)]:
        u, p, out = dialog(n)
        browser.KILL_SWITCH, real = stop, browser.KILL_SWITCH
        try:
            with eis.session(u) as kb:
                on_stroke(kb, 4, trigger)
                kb.type(TEXT)
            raise AssertionError(f"{name} no cortó")
        except (eis.Aborted, browser.Cancelled) as e:
            assert getattr(e, "sent", 8) == 8, e
        finally:
            browser.KILL_SWITCH = real
            eis.ABORT.clear()
            stop.unlink(missing_ok=True)
        assert submit(u, p, out) == TEXT[:8] + "\n", (name, out.read_text())

    # N6: otra ventana roba el foco durante el primer bloque
    (ua, pa, outa), (ub, pb, outb) = dialog(n), dialog(n)
    kwin.activate(ua)
    thief = threading.Thread(target=kwin.activate, args=(ub,))
    try:
        with eis.session(ua) as kb:
            on_stroke(kb, 2, thief.start)
            on_stroke(kb, 8, thief.join)
            kb.type(TEXT)
        raise AssertionError("el robo de foco no se notó")
    except eis.FocusLost as e:
        assert e.sent == 8, e.sent
    assert pb.poll() is None, "B recibió un Intro"
    b, a = submit(ub, pb, outb), (kwin.activate(ua), submit(ua, pa, outa))[1]
    print(f"N6: A recibió {a[:-1]!r}, B recibió {b[:-1]!r}")
    assert 1 <= len(b) - 1 <= 8 and a[:-1] + b[:-1] == TEXT[:8], (a, b)

    # N7: captura. Por uuid el recorte es el cliente de la ventana roja y su centro es rojo.
    p = n.launch(["/usr/bin/python3", "-I", str(Path(__file__).resolve().parent / "fixtures" / "red.py")])
    deadline = time.monotonic() + 20
    while not (w := [w for w in kwin.state()["windows"] if w["pid"] == p.pid]):
        assert time.monotonic() < deadline, "la ventana roja no apareció"
        time.sleep(0.1)
    u = w[0]["uuid"]

    def shot(target, **kw):
        t = time.monotonic()
        jpg, meta = capture.grab(target, **kw)
        return Image.open(io.BytesIO(jpg)), meta, (time.monotonic() - t) * 1000

    def red(img, xy=None):
        px = img.getpixel(xy or (img.width // 2, img.height // 2))
        return all(abs(a - b) <= 8 for a, b in zip(px, (255, 0, 0))), px

    deadline = time.monotonic() + 5
    while not red((img := shot(u))[0])[0]:  # el primer frame puede llegar sin pintar
        assert time.monotonic() < deadline, red(img[0])
        time.sleep(0.2)
    img, meta, _ = img
    c = next(w for w in kwin.state()["windows"] if w["uuid"] == u)["client"]
    assert meta["backend"] == "screenshot2" and meta["occluded_by"] == [] and meta["factor"] == 1, meta
    assert img.size == (meta["width"], meta["height"]) == (round(c[2]), round(c[3])), (img.size, c)
    assert meta["origin"] == [round(c[0]), round(c[1])], (meta, c)
    ms_ss2 = [shot("screen")[2] for _ in range(5)]
    for kw, size in [({}, (1280, 800)), ({"max_edge": 640}, (640, 400))]:
        img, meta, _ = shot("screen", **kw)
        assert img.size == size and red(img, (round(c[0] + c[2] / 2) * size[0] // 1280,
                                               round(c[1] + c[3] / 2) * size[0] // 1280))[0], (kw, img.size)

    # Space: un punto de la imagen reducida (factor 2) → EIS → el cursor de KWin, y el id caduca
    img, meta, _ = shot(u, max_edge=max(round(c[2]), round(c[3])) // 2)
    sp = spaces.Space(meta["space"], tuple(meta["origin"]), meta["factor"], meta["capture_id"])
    assert abs(sp.factor - 2) < 0.02, sp
    x, y = sp.click(51, 37, kwin.state()["outputs"])
    with eis.session(None) as kb:
        kb.move(x, y)
        kb.sync()
    cur = kwin.state()["cursor"]
    assert abs(cur[0] - x) <= 1 and abs(cur[1] - y) <= 1, (cur, x, y)
    shot("screen")
    try:
        sp.click(51, 37)
        raise AssertionError("una captura vieja sigue valiendo")
    except spaces.StaleCapture:
        pass

    # Spectacle forzado: el D-Bus de ScreenShot2 falla y la reserva da la misma imagen
    rc = Path.home() / ".config" / "spectaclerc"
    rc0 = rc.exists() and hashlib.md5(rc.read_bytes()).hexdigest()
    def broken(fd):
        raise dbus.DBusException("forzado")
    real, kwin.capture_workspace = kwin.capture_workspace, broken
    try:
        img, meta, ms_sp = shot(u)
    finally:
        kwin.capture_workspace = real
    assert meta["backend"] == "spectacle" and red(img)[0] and img.size == (round(c[2]), round(c[3])), (meta, red(img))
    assert (rc.exists() and hashlib.md5(rc.read_bytes()).hexdigest()) == rc0, "spectacle tocó ~/.config/spectaclerc"
    print(f"captura de pantalla: screenshot2 {min(ms_ss2):.0f}-{max(ms_ss2):.0f} ms, spectacle {ms_sp:.0f} ms")

    # occluded_by y NotVisible
    k = next(w for w in kwin.state()["windows"] if w["resource_class"] == "org.kde.kcalc")
    kwin.window_set(k["uuid"], geometry=(c[0], c[1], *k["frame"][2:]))
    kwin.activate(k["uuid"])
    assert shot(u)[1]["occluded_by"] == ["org.kde.kcalc"], shot(u)[1]
    kwin.window_set(u, minimized=True)
    try:
        shot(u)
        raise AssertionError("capturó una ventana minimizada")
    except capture.NotVisible:
        pass

assert kwin.BUS == "--user"
time.sleep(1)  # systemd-coredump tarda un momento en registrar
assert CORES().count("kwin_wayland") == CORES0 and f" {n.pid} " not in CORES(), "KWin anidada dejó un coredump"
print("test_desktop_nest OK")
