"""Una KWin anidada y aislada, para probar el control del escritorio sin tocar el de verdad.

    with nested(1280, 800) as n:
        n.launch(["kcalc"])
        kwin.wait_window("org.kde.kcalc")  # mientras dura el bloque, kwin.py habla con la anidada

Es infraestructura de test y nada más: el producto no anida compositor (D3). Existe porque probar
contra la sesión en vivo ya salió caro: una llamada D-Bus mal formada a kglobalaccel abortó
kwin_wayland y tiró la sesión entera, y una KWin anidada sin XDG propios escribió en el kwinrc y el
kwinoutputconfig.json reales.

Cada pieza del aislamiento está por algo:
  - Bus de sesión propio, con su socket en `bus` dentro de un XDG_RUNTIME_DIR propio (0700), igual que
    en la sesión real. Un cliente que ignore DBUS_SESSION_BUS_ADDRESS o WAYLAND_DISPLAY cae en el bus
    anidado o en nada: en ese runtime no hay ningún `wayland-0` que alcanzar.
  - XDG_CONFIG/DATA/CACHE/STATE_HOME propios. STATE también: KF6 guarda ahí el estado de las ventanas.
  - Entorno por lista blanca. SESSION_MANAGER, XAUTHORITY, ICEAUTHORITY o XDG_CONFIG_DIRS (que lleva
    ~/.config/kdedefaults) apuntan a la sesión en vivo, y una lista negra siempre olvida alguno.
  - Cada proceso en su propio scope de systemd, y el apagado para los scopes: así barre también lo
    que el bus anidado haya activado por su cuenta. Nunca se mata nada por nombre.
  - Durante el bloque, el os.environ del propio proceso ES el anidado (y se restaura al salir): lo que
    resuelva la sesión en proceso o lance hijos con {**os.environ} cae en la anidada, no en la real.
    Ojo: una conexión abierta ANTES del bloque (el SessionBus() cacheado de dbus-python) sigue en vivo;
    el código que hable D-Bus en proceso la abre dentro del bloque o con BusConnection(n.address).
  - Ni bus de sistema: DBUS_SYSTEM_BUS_ADDRESS apunta a un socket que no existe.
  - SIGTERM pasa por el mismo apagado. Lo que deje un SIGKILL (scopes y directorio, con el pid del
    dueño en el nombre) lo barre la siguiente ejecución en cuanto ese pid ya no existe.

Al salir, pase lo que pase dentro, comprueba lo que promete y lanza RuntimeError si no se cumple: los
md5 de la configuración KDE en vivo, el pid de la KWin en vivo, y ningún scope suyo vivo.

Autocomprobación:  uv run --project . tests/test_nest.py
"""

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import kwin

PROTECTED = [Path.home() / ".config" / n for n in (  # los mismos que vigila el hook kwin-guard
    "kwinrc", "kwinoutputconfig.json", "kglobalshortcutsrc", "kdeglobals", "kwinrulesrc", "kxkbrc", "kcminputrc",
    "plasmashellrc", "plasma-org.kde.plasma.desktop-appletsrc", "ksmserverrc")]
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ")  # lo único que se hereda del entorno vivo
SOCKET = "wayland-nest"


def md5s() -> dict:
    """md5 de la configuración KDE en vivo; None para un fichero que no existe (y no debe aparecer)."""
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest() if p.exists() else None for p in PROTECTED}


def kwin_pid(bus: str = "--user") -> int | None:
    """El pid dueño de org.kde.KWin en ese bus. `status` pregunta al demonio del bus, no a KWin."""
    out = subprocess.run(["busctl", bus, "status", "org.kde.KWin"], capture_output=True, text=True).stdout
    pid = [line[4:] for line in out.splitlines() if line.startswith("PID=")]
    return int(pid[0]) if pid else None


def _cgroup(unit: str) -> Path:
    uid = os.getuid()
    return Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/{unit}.scope")


def _sweep() -> None:
    """Para los scopes y borra los directorios de ejecuciones cuyo dueño ya murió (SIGKILL, doble Ctrl-C).
    Solo los nuestros: `manos-nest-<pid>-...`, y solo si <pid> ya no existe."""
    dead = lambda name: not Path(f"/proc/{name.split('-')[2]}").exists()
    out = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", "manos-nest-*"],
                         capture_output=True, text=True).stdout
    for unit in (line.split()[0] for line in out.splitlines()):
        if dead(unit):
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
    for d in Path(tempfile.gettempdir()).glob("manos-nest-*"):
        if dead(d.name):
            shutil.rmtree(d, ignore_errors=True)


class Nest:
    def __init__(self, root: Path, env: dict):
        self.root, self.env = root, env
        self.live = dict(os.environ)  # systemd-run lo necesita, y el finally lo restaura
        self.address = env["DBUS_SESSION_BUS_ADDRESS"]
        self.log = root / "kwin.log"  # el stderr de la KWin anidada: de ahí lee kwin.py las respuestas
        self.name = f"manos-nest-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.units, self.procs = [], []

    def _scoped(self, role: str, argv: list[str], env: dict) -> subprocess.Popen:
        # systemd-run necesita el entorno vivo para hablar con el gestor de usuario; `env -i` deja al
        # proceso solo con el anidado. Ambos hacen exec, así que el pid devuelto es el del proceso.
        unit = f"{self.name}-{role}"
        with open(self.root / f"{role}.log", "ab") as out:
            p = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
                                  "-p", "TimeoutStopSec=5", "--", "env", "-i",
                                  *(f"{k}={v}" for k, v in env.items()), *argv],
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=self.live)
        self.units.append(unit)
        self.procs.append(p)
        return p

    def launch(self, argv: list[str]) -> subprocess.Popen:
        """Arranca un cliente dentro de la sesión anidada. El apagado lo para junto con todo lo demás."""
        return self._scoped(f"c{len(self.units)}", argv, self.env)


def _wait(what: str, ok, proc: subprocess.Popen, log: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not ok():
        if proc.poll() is not None or time.monotonic() >= deadline:
            tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
            raise RuntimeError(f"la sesión anidada no arrancó: {what}\n{tail}")
        time.sleep(0.05)


@contextmanager
def nested(width: int = 1280, height: int = 800, layout: str = "es", xwayland: bool = False):
    _sweep()
    before, live = md5s(), kwin_pid()
    root = Path(tempfile.mkdtemp(prefix=f"manos-nest-{os.getpid()}-"))
    for d in ("run", "config", "data", "cache", "state"):
        (root / d).mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k in KEEP or k.startswith("LC_")}
    env.update(XDG_RUNTIME_DIR=f"{root}/run", XDG_CONFIG_HOME=f"{root}/config", XDG_DATA_HOME=f"{root}/data",
               XDG_CACHE_HOME=f"{root}/cache", XDG_STATE_HOME=f"{root}/state",
               DBUS_SESSION_BUS_ADDRESS=f"unix:path={root}/run/bus", WAYLAND_DISPLAY=SOCKET,
               DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={root}/run/nosystem",
               QT_QPA_PLATFORM="wayland",
               # Sin terminal de control, Qt manda el log al diario EN VIVO (medido: el print() del script
               # aterrizó allí, en el scope de la KWin anidada) y no al stderr del que lee kwin.py.
               QT_FORCE_STDERR_LOGGING="1")
    n = Nest(root, env)
    aimed = kwin.BUS, kwin._JOURNAL
    term = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # que un SIGTERM también pase por el finally
    try:
        live_run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        leaks = [k for k, v in env.items() if live_run in v or str(Path.home() / ".config") in v]
        if leaks or n.address == os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise RuntimeError(f"el entorno anidado apunta a la sesión en vivo: {leaks or n.address}")

        bus = n._scoped("bus", ["dbus-daemon", "--session", "--nofork", f"--address={n.address}"], env)
        _wait("dbus-daemon", lambda: subprocess.run(["busctl", f"--address={n.address}", "list"],
                                                    capture_output=True).returncode == 0, bus, root / "bus.log")
        kw = n._scoped("kwin", ["kwin_wayland", "--virtual", "--no-lockscreen", "--no-global-shortcuts",
                                "--no-kactivities", "--width", str(width), "--height", str(height),
                                "--socket", SOCKET, *(["--xwayland"] if xwayland else [])],
                       {**env, "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1", "KWIN_SCREENSHOT_NO_PERMISSION_CHECKS": "1",
                        "KWIN_XKB_DEFAULT_KEYMAP": "1", "XKB_DEFAULT_LAYOUT": layout})
        _wait("kwin_wayland", lambda: kwin_pid(f"--address={n.address}") == kw.pid, kw, n.log)
        n.pid = kw.pid
        kwin.BUS, kwin._JOURNAL = f"--address={n.address}", ["tail", "-n", "2000", str(n.log)]
        os.environ.clear()
        os.environ.update(env)
        yield n
    finally:
        signal.signal(signal.SIGTERM, term)
        os.environ.clear()
        os.environ.update(n.live)
        kwin.BUS, kwin._JOURNAL = aimed
        for unit in reversed(n.units):  # clientes, KWin y bus, en ese orden: KWin sin bus puede abortar
            subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True)
        for p in n.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass  # el chequeo de cgroups de abajo lo denuncia; no saltarse por ello el resto
        shutil.rmtree(root, ignore_errors=True)
        now = md5s()
        broken = [f"scope {u} sigue vivo" for u in n.units if _cgroup(u).exists()]
        broken += [f"~/.config/{k} cambió ({before[k]} -> {now[k]})" for k in before if now[k] != before[k]]
        if kwin_pid() != live:
            broken.append(f"la KWin en vivo cambió de pid ({live} -> {kwin_pid()})")
        if broken:
            raise RuntimeError("AISLAMIENTO ROTO de la sesión anidada: " + "; ".join(broken))
