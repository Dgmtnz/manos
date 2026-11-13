"""Meta+Shift+B en KWin anidada:  uv run --project . tests/test_summon_nest.py

`panic/manos-summon` de punta a punta con el entorno de la anidada: sin navegador avisa, sale con error y no deja
el fichero de traspaso; con una ventana GTK que hace de navegador (resourceClass ManosBrowser) aparcada fuera de
pantalla y minimizada (y un emergente igual), las deja en pantalla, la de arriba activa, y escribe `$XDG_DATA_HOME/manos/driving` en el
directorio de la anidada. `kwin.summon(activate=False)` (el de browser_handoff) las pone en pantalla sin
quitar el foco. `notify-send` es un doble que apunta lo que le llega: en la anidada no hay servidor de
notificaciones y el de verdad esperaría la activación D-Bus.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kwin
from browser import session as bs
from browser.session import BrowserSession
from nest import nested

HERE = Path(__file__).resolve().parent
FIX = str(HERE / "fixtures/ventana.py")
SCRIPT = HERE.parent / "panic/manos-summon"
CORES = lambda: subprocess.run(["coredumpctl", "list", "--no-legend"], capture_output=True, text=True).stdout
CORES0 = CORES().count("kwin_wayland")
LIVE_FLAG = Path.home() / ".local/share/manos/driving"
LIVE0 = LIVE_FLAG.exists()


def wait(cond, what, timeout=20):
    deadline = time.monotonic() + timeout
    while not (got := cond()):
        assert time.monotonic() < deadline, what
        time.sleep(0.1)
    return got


with tempfile.TemporaryDirectory() as stub, nested(1280, 800, "es") as n:
    said = Path(stub) / "said"
    Path(stub, "notify-send").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{said}"\n')
    Path(stub, "notify-send").chmod(0o755)
    env = {**n.env, "MANOS_KWIN_BUS": n.address, "MANOS_KWIN_LOG": str(n.log), "PATH": f"{stub}:{n.env['PATH']}"}
    flag = Path(n.env["XDG_DATA_HOME"]) / "manos/driving"
    summon = lambda: subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)

    r = summon()  # sin navegador
    assert r.returncode == 1 and not flag.exists(), (r.returncode, r.stdout, r.stderr)
    assert "no hay navegador" in said.read_text(), said.read_text()

    # El domador, como BrowserSession.start(): cargado antes de la ventana, y aparcada como park() (sin minimizar).
    tamer = kwin.load_tamer("ManosBrowser")
    n.launch(["/usr/bin/python3", FIX, "ManosBrowser"])
    u = kwin.wait_window("ManosBrowser")["uuid"]
    park = lambda w: kwin.window_set(w, (3400.0, 100.0, 600.0, 400.0), minimized=False, skipTaskbar=True,
                                     skipSwitcher=True, skipPager=True, excludeFromCapture=True)
    park(u)
    assert kwin.active() != u, "la ventana del navegador quedó activa al nacer"

    def close_active(*keep):
        """Abre una ventana, la activa y la cierra: KWin activa la siguiente de su cadena de foco."""
        p = n.launch(["/usr/bin/python3", FIX, "efimera"])
        e = wait(lambda: next((w["uuid"] for w in kwin.windows("efimera")), None), "efimera no apareció")
        kwin.activate(e)
        p.terminate()
        wait(lambda: not kwin.windows("efimera"), "efimera no se cerró")
        time.sleep(0.5)
        return kwin.active()

    # Sin el arreglo, los tres casos acababan con el navegador aparcado activo (medido en anidada).
    assert close_active() != u, "sola: el navegador aparcado heredó el foco"
    p_otra = n.launch(["/usr/bin/python3", FIX, "dos"])
    dos = kwin.wait_window("dos")["uuid"]
    kwin.window_set(dos, None, minimized=True)
    assert close_active() != u, "con la otra minimizada: el navegador aparcado heredó el foco"
    kwin.window_set(dos, None, minimized=False)
    kwin.activate(dos)
    assert close_active() == dos, "con otra visible: el foco no volvió a la última activa"
    # un emergente del agente (otra ventana de la clase) tampoco se queda el foco
    n.launch(["/usr/bin/python3", FIX, "ManosBrowser"])
    pop0 = wait(lambda: next((w["uuid"] for w in kwin.windows("ManosBrowser") if w["uuid"] != u), None), "sin emergente")
    time.sleep(0.5)
    assert kwin.active() not in (u, pop0), kwin.active()
    p_otra.terminate()
    wait(lambda: not kwin.windows("dos"), "dos no se cerró")
    wait(lambda: kwin.active() not in (u, pop0), "el navegador heredó el foco al cerrarse la última")
    print("foco: con el domador, ninguna activación se queda en el navegador aparcado")
    kwin.window_set(pop0, None, minimized=True)

    kwin.window_set(u, (3400.0, 100.0, 600.0, 400.0), minimized=True, skipTaskbar=True, skipSwitcher=True)
    n.launch(["/usr/bin/python3", FIX, "otra"])
    other = kwin.wait_window("otra")["uuid"]
    wait(lambda: kwin.active() == other, "la otra ventana no quedó activa")
    info = kwin.window_info(u)
    # aparcada: minimizada y fuera de pantalla (la anidada de una salida deja 100 px dentro: x=1180, medido)
    assert info["minimized"] and info["x"] + info["width"] > 1280, info

    # un emergente («Iniciar sesión con Google») también aparcado: el traspaso los trae todos
    popup = pop0
    kwin.window_set(popup, (3400.0, 300.0, 400.0, 300.0), minimized=True)
    wait(lambda: kwin.active() == other, "la otra ventana no recuperó el foco")

    # browser_handoff: en pantalla pidiendo atención, sin quitarle el foco a lo que Diego tenga delante
    got = kwin.summon("ManosBrowser", activate=False)
    assert len(got["windows"]) == 2 and not got["active"] and kwin.active() == other, got
    assert got["attention"], got  # getWindowInfo no trae demandsAttention: lo relee el propio script
    for w in (u, popup):
        kwin.window_set(w, (3400.0, 100.0, 600.0, 400.0), minimized=True)

    t0 = time.perf_counter()
    r = summon()
    ms = (time.perf_counter() - t0) * 1000
    assert r.returncode == 0, (r.stdout, r.stderr)
    info = kwin.window_info(u)
    assert not info["minimized"] and kwin.active() in (u, popup), (info, kwin.active())  # Meta+Shift+B sí activa
    assert not kwin.window_info(popup)["minimized"], kwin.window_info(popup)
    assert 0 <= info["x"] and info["x"] + info["width"] <= 1280 and 0 <= info["y"] and info["y"] + info["height"] <= 800, info
    assert json.loads(flag.read_text())["reason"] == "Diego pulsó Meta+Shift+B y tomó el navegador", flag.read_text()
    assert "el navegador es tuyo" in said.read_text(), said.read_text()
    print(f"summon: {ms:.0f} ms de punta a punta, ventana en {[info[k] for k in ('x', 'y', 'width', 'height')]}")

    r = summon()  # con el fichero ya puesto: lo deja, y vuelve a traerla
    assert r.returncode == 0 and flag.exists(), (r.stdout, r.stderr)
    assert not kwin.is_loaded("manostamer"), "Meta+Shift+B dejó cargado el domador"

    # take_back: el domador otra vez y las ventanas aparcadas; cerrar la activa no despierta al navegador
    os.unlink(tamer)
    tamer = kwin.load_tamer("ManosBrowser")
    for w in (u, popup):
        park(w)
    assert close_active() not in (u, popup), "tras recargar el domador, el navegador heredó el foco"
    # modo mirar (browser_open visible=true): control compartido. Una BrowserSession sin Chrome sobre la ventana falsa.
    async def mirar():
        bs.PARKED_X = 1180.0  # la anidada de una salida deja 100 px dentro (ver arriba)
        s = BrowserSession(None, SimpleNamespace(pages=[]), "nest", tamer)
        s.window_uuid = u
        await s.set_visible(True)  # en el área útil, en la barra, sin activarla y sin domador
        info, (ax, ay, aw, ah) = kwin.window_info(u), kwin.work_area()
        assert ax <= info["x"] and info["x"] + info["width"] <= ax + aw and ay <= info["y"] and info["y"] + info["height"] <= ay + ah, info
        assert not info["minimized"] and not info["skipTaskbar"] and info["excludeFromCapture"] and kwin.active() != u, info
        assert not kwin.is_loaded("manostamer") and not os.path.exists(tamer), "a la vista sigue el domador"
        kwin.activate(u)  # Diego hace clic en ella: se queda activa
        await asyncio.sleep(0.5)
        assert kwin.active() == u, "a la vista, la activación del navegador se deshizo"
        kwin.activate(other)
        await s.hand_over()  # browser_resume a la vista: take_back no vuelve a cargar el domador
        await s.take_back()
        assert not kwin.is_loaded("manostamer") and not s._handed, "browser_resume a la vista cargó el domador"
        await s.set_visible(False)  # escondida: el domador otra vez, y una activación se deshace
        assert kwin.is_loaded("manostamer") and kwin.window_info(u)["skipTaskbar"], kwin.window_info(u)
        with contextlib.suppress(kwin.KWinError):  # el domador la deshace dentro de la misma llamada
            kwin.activate(u)
        await asyncio.sleep(0.5)
        assert kwin.active() != u, "escondida, la activación del navegador se quedó"
        return s, info

    s, info = asyncio.run(mirar())
    print(f"modo mirar: {[info[k] for k in ('x', 'y', 'width', 'height')]}, sin domador y la activación se queda; "
          "escondida, el domador la deshace")
    kwin.unload("manostamer")
    os.unlink(s._tamer_path)

assert LIVE_FLAG.exists() == LIVE0, "escribió el fichero de traspaso en vivo"
time.sleep(1)  # systemd-coredump tarda un momento en registrar
assert CORES().count("kwin_wayland") == CORES0, "KWin anidada dejó un coredump"
print("test_summon_nest OK")
