"""Autocomprobación de tests/nest.py y de kwin.py apuntado a ella:  uv run --project . tests/test_nest.py

Arranca una KWin anidada, abre dentro un cliente Qt ligero, lo encuentra con `kwin.windows()`, lo mueve
y lo redimensiona con `kwin.window_set` y relee la geometría, y mira que ScreenShot2 esté en el bus
privado. Al salir comprueba que la sesión en vivo sigue intacta y que no sobrevive ningún proceso.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kwin
from nest import _cgroup, kwin_pid, md5s, nested

BEFORE, LIVE, ENV = md5s(), kwin_pid(), dict(os.environ)
LIVE_RUN = f"/run/user/{os.getuid()}"

# Restos de una ejecución muerta por SIGKILL: un scope y un directorio con el pid de un proceso que ya no existe.
dead = subprocess.Popen(["true"]); dead.wait()
orphan = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--collect",
                           f"--unit=manos-nest-{dead.pid}-000000-c0", "sleep", "300"])
stale = Path(tempfile.gettempdir()) / f"manos-nest-{dead.pid}-x"
stale.mkdir()
print("md5 antes:   ", BEFORE)
print("KWin en vivo:", LIVE)

with nested(1280, 800) as n:
    assert orphan.wait(timeout=10) is not None and not stale.exists(), "el barrido no limpió los restos"
    assert n.address != ENV["DBUS_SESSION_BUS_ADDRESS"], n.address
    # El propio proceso también está dentro: os.environ es el anidado, y un hijo con el entorno heredado
    # (`busctl --user`) encuentra la KWin anidada, no la de verdad.
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == n.address and "DISPLAY" not in os.environ
    assert not [k for k, v in os.environ.items() if LIVE_RUN in v or str(Path.home() / ".config") in v]
    assert kwin_pid() == n.pid, (kwin_pid(), n.pid)
    assert kwin.BUS == f"--address={n.address}", kwin.BUS
    assert n.pid != LIVE and kwin_pid(kwin.BUS) == n.pid, (n.pid, LIVE)
    print(f"KWin anidada: pid {n.pid} en {n.address}")
    assert "CaptureWorkspace" in kwin._busctl("introspect", "org.kde.KWin", "/org/kde/KWin/ScreenShot2",
                                              "org.kde.KWin.ScreenShot2")

    n.launch(["kcalc"])
    win = kwin.wait_window("org.kde.kcalc")
    assert [w["uuid"] for w in kwin.windows("org.kde.kcalc")] == [win["uuid"]]
    for want in ([100.0, 80.0, 600.0, 500.0], [300.0, 200.0, 500.0, 450.0]):
        got = kwin.window_set(win["uuid"], want)
        assert got["geometry"] == want, (got, want)
        print(f"window_set {want}: {got['geometry']} (antes {got['before']})")
    root, procs, units = n.root, n.procs, n.units

assert kwin.BUS == "--user" and kwin._JOURNAL[0] == "journalctl", (kwin.BUS, kwin._JOURNAL)
assert dict(os.environ) == ENV, "os.environ no se restauró"
assert not root.exists(), root
assert all(p.returncode is not None for p in procs), [p.args[:6] for p in procs if p.returncode is None]
assert not any(_cgroup(u).exists() for u in units), units
AFTER = md5s()
print("md5 después: ", AFTER)
assert AFTER == BEFORE and kwin_pid() == LIVE, (AFTER, kwin_pid())
print("test_nest OK")
