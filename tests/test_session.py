"""Autocomprobación de kwin.py y browser/session.py:  uv run --project . tests/test_session.py

Lanza el navegador de verdad: arranca, comprueba que no abrió ningún socket a la escucha, que KWin lo
colocó fuera de pantalla con el resourceClass correcto, le manda un clic por CDP y lo lee de vuelta
desde la página, y lo cierra sin dejar procesos. De paso mide arranque y memoria.

Imprime el md5 de los cuatro ficheros de configuración de KDE antes y después: este test no debe
tocarlos, y la única forma de saberlo es mirar.
"""

import asyncio
import hashlib
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kwin
from browser import session as bs
from browser.session import (PARKED_SIZE, PARKED_X, RESOURCE_CLASS, BrowserSession, Busy, Cancelled,
                             DialogOpen, KILL_SWITCH, _browser_main, _seed_prefs, PROFILE, check,
                             renderer_seccomp)

CONFIGS = [Path.home() / ".config" / n
           for n in ("kwinrc", "kwinrulesrc", "kdeglobals", "kglobalshortcutsrc")]
FIXTURE = Path(__file__).parent / "click.html"
FIXTURE.write_text("""<!doctype html><title>manos</title>
<body style="margin:0"><div id=t style="width:100vw;height:100vh;background:#eee"></div>
<script>window.__hit=null;document.getElementById('t').addEventListener('click',
  e=>{window.__hit=[e.clientX,e.clientY]});</script>""")


def md5s():
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in CONFIGS if p.exists()}


def detached(coro):
    """Lanza una corrutina que no va a retornar (un alert abierto, un Page.crash) y se come su error."""
    task = asyncio.ensure_future(coro)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


def frozen(cgroup, tries=200):
    """Espera a que el cgroup quede congelado. La escritura de `cgroup.freeze` retorna en menos de
    1 ms pero la transición `frozen 0→1` de `cgroup.events` se midió en 10,2 ms: leerla en el acto es
    una carrera, no una comprobación."""
    for _ in range(tries):
        if "frozen 1" in cgroup.joinpath("cgroup.events").read_text():
            return True
        time.sleep(0.01)
    return False


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    return False


# --- lo que no necesita navegador -------------------------------------------------------------------

assert raises(ValueError, kwin.window_set, "no-soy-un-uuid")
assert raises(ValueError, kwin.window_set, "11111111-2222-3333-4444-555555555555", noExisteEstaProp=1)
assert raises(ValueError, kwin.window_info, "'; workspace.quit(); //")  # la interpolación es un límite

_seed_prefs()
_prefs = json.loads((PROFILE / "Default" / "Preferences").read_text())
assert _prefs["credentials_enable_service"] is False
assert _prefs["profile"]["password_manager_leak_detection"] is False
assert _prefs["autofill"]["credit_card_enabled"] is False
assert "enabled" not in _prefs["autofill"]  # autofill.enabled está deprecado y no se siembra
_seed_prefs()  # idempotente
assert json.loads((PROFILE / "Default" / "Preferences").read_text()) == _prefs

# _browser_main sobre un /proc falso: dentro, fuera, y un padre que no es Chrome (Brave) no cuenta.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "chrome").touch(); (root / "brave").touch(); (root / "cg").mkdir()

    def fake(pid, ppid, exe, cmdline):
        d = root / "proc" / pid
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} (x) S {ppid} 0 0")
        (d / "exe").symlink_to(root / exe)
        (d / "cmdline").write_text(cmdline)

    fake("100", "1", "chrome", "chrome --class=ManosBrowser")
    fake("101", "100", "chrome", "chrome --type=zygote")
    fake("200", "1", "brave", "brave")
    fake("201", "200", "chrome", "chrome --type=renderer")
    procs = root / "cg" / "cgroup.procs"
    main = lambda: _browser_main(root / "cg", root / "proc", root / "chrome")
    procs.write_text("100\n101\n"); assert main() == ("100", False)  # nunca se mudó
    procs.write_text("101\n");       assert main() == ("100", True)   # se mudó: hay que devolverlo
    procs.write_text("201\n");       assert main() is None            # el padre es Brave: jamás

check()  # sin kill-switch no hace nada
assert not KILL_SWITCH.exists(), f"hay un kill-switch puesto en {KILL_SWITCH}"
KILL_SWITCH.write_text("test")
assert raises(Cancelled, check)  # el freno no depende de ninguna petición: es un fichero
KILL_SWITCH.unlink()

_live = kwin.windows()
assert _live and all(w["uuid"] == w["uuid"].strip("{}") for w in _live)
assert len(_live) == len({w["uuid"] for w in _live}), "Match('') devuelve cada ventana dos veces"
assert not kwin.windows(RESOURCE_CLASS), "ya hay un ManosBrowser suelto de una ejecución anterior"

BEFORE = md5s()
print("md5 antes: ", json.dumps(BEFORE, indent=None))


# --- el navegador de verdad ---------------------------------------------------------------------------

async def main():
    t0 = time.monotonic()
    s = await BrowserSession.start()
    launch_ms = (time.monotonic() - t0) * 1000
    try:
        pids = s.cgroup.joinpath("cgroup.procs").read_text().split()
        pss = sum(int(l.split()[1]) for p in pids
                  for l in Path(f"/proc/{p}/smaps_rollup").read_text().splitlines()
                  if l.startswith("Pss:"))
        print(f"arranque {launch_ms:.0f} ms · {len(pids)} procesos · {pss / 1024:.0f} MB PSS")

        out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True).stdout
        assert not any(f"pid={p}," in out for p in pids), "el navegador abrió un socket a la escucha"

        # El sandbox, leído del kernel: Playwright lo apaga por defecto e inyecta --no-sandbox, y
        # entonces los renderers salen con Seccomp 0. Con chromium_sandbox=True salen con 2.
        seccomp = renderer_seccomp(s.cgroup)
        assert seccomp and set(seccomp) == {"2"}, f"renderers sin sandbox: Seccomp={seccomp}"
        print(f"sandbox de Chromium: Seccomp={seccomp} en {len(seccomp)} renderers")

        info = kwin.window_info(s.window_uuid)
        assert info["resourceClass"] == RESOURCE_CLASS, info["resourceClass"]
        assert info["x"] >= PARKED_X, f"la ventana no está fuera de pantalla: x={info['x']}"
        assert info["skipTaskbar"] and info["skipSwitcher"] and info["skipPager"]
        assert info["excludeFromCapture"] and not info["minimized"]
        assert (info["width"], info["height"]) == PARKED_SIZE, "park() no fijó PARKED_SIZE"
        # Un resize con la ventana fuera de toda salida SÍ pega —8 de 8 medidos—, al contrario de lo
        # que afirmaba la medida anterior. De ahí que park() escriba el tamaño en vez de heredar el
        # que el perfil recuerde, que es lo que hacía ir a la deriva el viewport del agente.
        smaller = (info["x"], info["y"], info["width"] - 100, info["height"] - 100)
        assert kwin.window_set(s.window_uuid, smaller)["geometry"] == list(smaller), "el resize no pegó"
        assert kwin.window_info(s.window_uuid)["width"] == info["width"] - 100
        await s.park()  # y el reposo lo deshace, así que correr este test ya no encoge la ventana
        assert kwin.window_info(s.window_uuid)["width"] == PARKED_SIZE[0], "park() no restauró el tamaño"
        print(f"ventana {info['resourceClass']} en "
              f"({info['x']:.0f},{info['y']:.0f}) {info['width']:.0f}x{info['height']:.0f} · "
              f"minimizada={info['minimized']} · excludeFromCapture={info['excludeFromCapture']}")

        await s.page.goto(FIXTURE.as_uri())
        cdp = await s.cdp()
        for kind in ("mousePressed", "mouseReleased"):
            await cdp.send("Input.dispatchMouseEvent",
                           {"type": kind, "x": 120, "y": 90, "button": "left", "clickCount": 1})
        hit = (await cdp.send("Runtime.evaluate",
                              {"expression": "window.__hit", "returnByValue": True}))["result"]["value"]
        assert hit == [120, 90], f"el clic no llegó a la página: {hit!r}"
        print(f"clic por CDP leído de vuelta en la página: {hit}")

        shot = await s.screenshot()
        assert shot[:2] == b"\xff\xd8", "la captura no es un JPEG"
        print(f"captura jpeg {len(shot) / 1024:.0f} KiB")

        # Una llamada a la vez por navegador: la segunda espera su turno hasta LOCK_WAIT y luego da Busy.
        # Dos observaciones solapadas se retiraban los refs la una a la otra.
        wait, bs.LOCK_WAIT = bs.LOCK_WAIT, 0.2
        async with s.exclusive():
            try:
                async with s.exclusive():
                    raise AssertionError("dos llamadas a la vez entraron en el mismo navegador")
            except Busy:
                pass
        bs.LOCK_WAIT = wait
        order = []

        async def turn(i):
            async with s.exclusive():
                order.append(i)
                await asyncio.sleep(0.1)
                order.append(i)
        await asyncio.gather(turn(1), turn(2))
        assert order == [1, 1, 2, 2], order  # y el candado se suelta al salir
        print("candado: dos llamadas concurrentes van por turnos; tras LOCK_WAIT, Busy")

        s.require_no_dialog()  # sin diálogo abierto no lanza
        detached(s.page.evaluate("alert('hola')"))  # no retorna hasta responder el diálogo
        for _ in range(50):
            if s.dialog is not None:
                break
            await asyncio.sleep(0.05)
        assert s.dialog is not None, "el manejador permanente de diálogos no vio el alert"
        assert raises(DialogOpen, s.require_no_dialog)
        await s.answer_dialog(accept=True)
        assert s.dialog is None
        print("diálogo abierto, detectado como DialogOpen y respondido")

        # Los frenos van ANTES de la caída del renderer, y es una medida: el volcado de core que
        # dispara `Page.crash` deja tareas del árbol en el kernel, y mientras duran el cgroup no
        # alcanza `frozen 1` (se esperó 2 s y no llegó). Congelar sobre un árbol sano son 50 ms.
        t = time.monotonic(); s.freeze(); f_ms = (time.monotonic() - t) * 1000
        assert frozen(s.cgroup), "el árbol no quedó congelado"
        t = time.monotonic(); s.thaw(); th_ms = (time.monotonic() - t) * 1000
        assert (await s.page.evaluate("1+1")) == 2, "el árbol no revivió tras descongelar"
        print(f"freeze {f_ms:.1f} ms · thaw {th_ms:.1f} ms · CDP vivo después")

        # El pánico (`panic/manos-panic`) congela el scope con `systemctl --user freeze`. Chrome se
        # muda solo a `app-org.chromium.Chromium-<pid>.scope`: si el proceso principal no está de
        # vuelta en el nuestro, el pánico congela a los hijos y deja vivo al que habla CDP. Se prueba
        # el mismo mecanismo sobre NUESTRA unidad por su nombre exacto, sin el fichero de parada ni
        # los scopes de otros servidores que el script de verdad tocaría.
        assert s.main_pid in s.cgroup.joinpath("cgroup.procs").read_text().split(), \
            f"el proceso principal {s.main_pid} no está en el scope de manos"
        unit = f"{s.scope}.scope"
        subprocess.run(["systemctl", "--user", "freeze", unit], check=True)
        assert frozen(s.cgroup), "el freeze del pánico no congeló el scope"
        state = subprocess.run(["systemctl", "--user", "show", "-P", "FreezerState", unit],
                               capture_output=True, text=True).stdout.strip()
        subprocess.run(["systemctl", "--user", "thaw", unit], check=True)
        assert state == "frozen", state
        assert (await s.page.evaluate("1+1")) == 2, "el árbol no revivió tras el thaw del pánico"
        # Y un solo --disable-features, con el nuestro y la lista de Playwright: Chrome se queda con
        # el último, y el nuestro tiraba la de Playwright entera.
        # Chrome reescribe su cmdline con espacios en vez de NUL (setproctitle).
        argv = Path(f"/proc/{s.main_pid}/cmdline").read_text().replace("\0", " ").split()
        feats = [a for a in argv if a.startswith("--disable-features=")]
        assert len(feats) == 1 and "AutofillServerCommunication" in feats[0] \
            and "ThirdPartyStoragePartitioning" in feats[0], feats
        on = [a for a in argv if a.startswith("--enable-features=")]
        assert len(on) == 1 and "WebMCP" in on[0] and "CDPScreenshotNewSurface" in on[0], on
        print(f"pánico: proceso principal {s.main_pid} dentro del scope, FreezerState={state}, "
              "thaw y CDP vivo · --disable-features y --enable-features fundidos en uno")

        # El freno muerde: check() congela ANTES de lanzar, así que tocar el kill-switch detiene una
        # llamada en vuelo y no solo impide empezar la siguiente.
        KILL_SWITCH.write_text("test")
        assert raises(Cancelled, check, s)
        assert frozen(s.cgroup), "el kill-switch no congeló el árbol"
        KILL_SWITCH.unlink()
        s.thaw()
        print("kill-switch: Cancelled con el árbol ya congelado")

        # Caída del renderer: Inspector.targetCrashed NO se dispara y Page.reload también da timeout,
        # así que se detecta por timeout y se recupera por la conexión del navegador, que sobrevive.
        # La caída es a propósito: RLIMIT_CORE=1 a los renderers, o deja un core por pasada (con 0
        # systemd-coredump lo apunta igual; docs/knowledge.md). Al proceso principal no: si el cierre
        # vuelve a tumbarlo, el core tiene que salir.
        for pid in s.cgroup.joinpath("cgroup.procs").read_text().split():
            if "--type=renderer" in Path(f"/proc/{pid}/cmdline").read_text():
                resource.prlimit(int(pid), resource.RLIMIT_CORE, (1, 1))
        detached(cdp.send("Page.crash"))  # esta llamada no retorna nunca
        await asyncio.sleep(1.0)
        page = await s.recover()
        await page.goto(FIXTURE.as_uri())
        assert await page.title() == "manos", "no se recuperó de la caída del renderer"
        print("renderer caído y recuperado con Target.close + Target.create")
        cgroup = s.cgroup
    finally:
        await s.close()

    for _ in range(50):
        if not cgroup.exists() or not cgroup.joinpath("cgroup.procs").read_text().strip():
            break
        await asyncio.sleep(0.1)
    left = [] if not cgroup.exists() else cgroup.joinpath("cgroup.procs").read_text().split()
    assert not left, f"quedaron procesos tras cerrar: {left}"
    assert not kwin.is_loaded("manostamer"), "el script domador se quedó cargado"
    assert not kwin.windows(RESOURCE_CLASS), "la ventana del navegador sigue en KWin"
    print("cerrado: 0 procesos, 0 scripts KWin cargados, 0 ventanas")


asyncio.run(main())

AFTER = md5s()
print("md5 después:", json.dumps(AFTER, indent=None))
assert AFTER == BEFORE, "¡este test tocó la configuración de KDE!"
FIXTURE.unlink()
print(f"test_session OK  ({kwin.__file__})")
