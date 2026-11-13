"""Peldaño 2: accesibilidad AT-SPI, con libatspi siempre en otro proceso.

`Atspi.init()` aborta el proceso con el bus de accesibilidad roto: aquí no se importa `gi`. La salud se
mira por D-Bus plano (`healthy`) y libatspi vive en `atspi_worker.py` (Python del sistema, JSON por
líneas). Si el trabajador muere o tarda más de 5 s: SIGKILL a su pid, `gen += 1`, refs olvidados y
`A11yDown`. Las lecturas se reintentan una vez con trabajador nuevo; las acciones nunca (`ActionUncertain`).
La cura del bus (`cure`) solo corre a mano (`server.py --cure-a11y`, D4): puede matar el lanzador de Gram.
Diseño: docs/p3-design.md §2, §3 y §6.

Autocomprobación (pura y en KWin anidada):  uv run --project . tests/test_atspi_nest.py
"""

import fcntl
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import dbus

import kwin
import refs
import spaces
from desktop import eis
from desktop.apps import AmbiguousWindow, NoSuchAction

WORKER = Path(__file__).with_name("atspi_worker.py")
LAUNCHER = "/usr/lib/at-spi-bus-launcher"
DEADLINE = 5.0  # s por respuesta del trabajador (libatspi espera 2 s por llamada)
FOCUS_WAIT = 0.3  # s que se espera a que el campo diga `focused` antes de teclear


class A11yDown(RuntimeError): pass
class ActionUncertain(RuntimeError): pass
class NotInA11y(RuntimeError): pass
class NoToplevel(RuntimeError): pass
class A11yEmpty(RuntimeError): pass


_ERRORS = {c.__name__: c for c in (NotInA11y, NoToplevel, A11yEmpty, AmbiguousWindow, NoSuchAction,
                                   refs.StaleHandle)}


# --- bus de accesibilidad: solo D-Bus plano ---------------------------------------------------------

def healthy(timeout: float = 2.0) -> str | None:
    """La dirección del bus a11y si el registro contesta a `Ping`, o None. Nada aquí puede abortar."""
    try:
        addr = os.environ.get("AT_SPI_BUS_ADDRESS")
        if not addr:
            s = dbus.bus.BusConnection(kwin.bus_address())
            try:
                addr = str(s.call_blocking("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", "", (),
                                           timeout=timeout))
            finally:
                s.close()
        a = dbus.bus.BusConnection(addr)
        try:
            a.call_blocking("org.a11y.atspi.Registry", "/org/a11y/atspi/registry", "org.freedesktop.DBus.Peer",
                            "Ping", "", (), timeout=timeout)
        finally:
            a.close()
        return addr
    except Exception:
        return None


def _status(conn, prop: str) -> bool:
    return bool(conn.call_blocking("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Get", "ss",
                                   ("org.a11y.Status", prop), timeout=2.0))


_enabled: set[str] = set()  # buses de sesión donde ya se miró IsEnabled


def enable_once() -> bool:
    """Pone `org.a11y.Status.IsEnabled` si está a false, una vez por bus. True si lo acaba de poner.

    Nunca se restaura: el propio cambio disparó la carrera del lanzador, y restaurarlo cegaría a otra
    instancia que lo necesite."""
    addr = kwin.bus_address()
    if addr in _enabled:
        return False
    conn = dbus.bus.BusConnection(addr)
    try:
        was = _status(conn, "IsEnabled")
        if not was:
            conn.call_blocking("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set", "ssv",
                               ("org.a11y.Status", "IsEnabled", dbus.Boolean(True)), timeout=2.0)
    finally:
        conn.close()
    _enabled.add(addr)
    return not was


# --- cura (solo a mano) -----------------------------------------------------------------------------

def _pids(comm: str, proc_root) -> list[tuple[int, str, Path]]:
    """(pid, cgroup, dir) de los procesos nuestros con ese `comm`, fuera de cualquier anidada de test."""
    out = []
    for p in Path(proc_root).iterdir():
        try:
            if not p.name.isdigit() or p.stat().st_uid != os.getuid() or (p / "comm").read_text().strip() != comm:
                continue
            cg = (p / "cgroup").read_text().strip().rpartition("::")[2]
        except OSError:
            continue  # murió mientras se leía
        if "manos-nest-" not in cg:
            out.append((int(p.name), cg, p))
    return out


def rogue_launchers(proc_root="/proc", keep: str | None = None) -> list[int]:
    """Lanzadores de bus a11y ajenos (el de Gram, por ejemplo): `comm` y `exe` del lanzador y un cgroup
    distinto del de `at-spi-dbus-bus.service` (`keep`). Nunca toca registryd ni una anidada de test."""
    if keep is None:
        keep = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value",
                               "at-spi-dbus-bus.service"], capture_output=True, text=True).stdout.strip()
    if not keep:
        return []  # sin el cgroup del servicio no se distingue el lanzador bueno de los ajenos
    found = []
    for pid, cg, p in _pids("at-spi-bus-laun", proc_root):
        try:
            if os.readlink(p / "exe") == LAUNCHER and cg != keep:
                found.append(pid)
        except OSError:
            pass
    return sorted(found)


def cure() -> str:
    """Pasos 1-4 de §6. Solo si el bus está roto, sin lector de pantalla y con la firma conocida."""
    if kwin.BUS != "--user":  # lanzadores, registryd y systemctl --user son de la sesión en vivo
        return "En una KWin anidada no se cura nada: la cura toca procesos de la sesión en vivo."
    run = Path(os.environ["XDG_RUNTIME_DIR"]) / "manos"
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(run / "a11y-cure.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "Ya hay otra cura en curso."
        if healthy():
            return "El bus de accesibilidad está sano: nada que curar."
        try:
            conn = dbus.bus.BusConnection(kwin.bus_address())
            try:
                reader = _status(conn, "ScreenReaderEnabled")
            finally:
                conn.close()
        except dbus.DBusException as e:
            return f"No se pudo leer ScreenReaderEnabled ({e}); no se toca nada."
        if reader:
            return "Hay un lector de pantalla activo: no se toca nada."
        rogue = rogue_launchers()
        if not rogue and len(_pids("at-spi2-registr", "/proc")) < 2:
            return "Fallo del bus de accesibilidad no reconocido: no se toca nada."
        for pid in rogue:
            os.kill(pid, signal.SIGTERM)
        subprocess.run(["systemctl", "--user", "restart", "at-spi-dbus-bus.service"], capture_output=True)
        deadline = time.monotonic() + 3
        while not (addr := healthy()) and time.monotonic() < deadline:
            time.sleep(0.2)
        if not addr:
            msg = f"Lanzadores parados {rogue} y servicio reiniciado, pero el bus sigue sin contestar."
        else:
            # registryd se elige por dueño del nombre en el bus nuevo, nunca por cgroup: el sano es de
            # una unidad activada por D-Bus, no de at-spi-dbus-bus.service
            a = dbus.bus.BusConnection(addr)
            try:
                keep = int(a.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                           "GetConnectionUnixProcessID", "s", ("org.a11y.atspi.Registry",),
                                           timeout=2.0))
            finally:
                a.close()
            extra = [pid for pid, _, _ in _pids("at-spi2-registr", "/proc") if pid != keep]
            for pid in extra:
                os.kill(pid, signal.SIGTERM)
            msg = (f"Bus de accesibilidad curado: lanzadores parados {rogue}, registryd de más {extra}. "
                   "Las apps Qt abiertas antes de esto necesitan reiniciarse.")
        print(msg, file=sys.stderr)
        subprocess.run(["notify-send", "manos", msg], capture_output=True)
        return msg


# --- el trabajador ----------------------------------------------------------------------------------

_proc: subprocess.Popen | None = None
_gen = 1
_buf = b""
_scopes: set[str] = set()  # ámbitos `atspi:<uuid>` registrados en refs.py
_lock = threading.RLock()  # ui_snapshot no pasa por el cerrojo del escritorio: una petición a la vez por la tubería


def _down(why: str) -> A11yDown:
    """Mata el trabajador (por su pid), sube la generación y olvida sus refs. Devuelve el error a lanzar."""
    global _proc, _gen, _buf
    rc = None
    with _lock:
        if _proc is not None:
            if _proc.poll() is None:
                _proc.kill()
            rc = _proc.wait()
            _proc.stdin.close()
            _proc.stdout.close()
        _proc, _buf, _gen = None, b"", _gen + 1
        for s in _scopes:
            refs.forget(s)
        _scopes.clear()
    return A11yDown(f"Accesibilidad caída: {why} (estado del trabajador {rc}). Usa app_actions o "
                    "screen_capture, y pide a Diego `server.py --cure-a11y` si sigue así.")


def _spawn() -> None:
    # adaptado de kwin-mcp core.py:504-550 (MIT): sesión propia y generación en la línea de órdenes
    global _proc
    addr = healthy()
    if not addr:
        raise A11yDown("El bus de accesibilidad no contesta. Usa app_actions o screen_capture, y pide a Diego "
                       "`server.py --cure-a11y`.")
    _proc = subprocess.Popen(["/usr/bin/python3", "-I", str(WORKER), "--generation", str(_gen)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
                             env={**os.environ, "AT_SPI_BUS_ADDRESS": addr}, start_new_session=True)


def _read() -> dict:
    # de kwin-mcp core.py:552-592 (MIT), recortado: una línea con plazo, EOF si el trabajador muere
    global _buf
    deadline, fd = time.monotonic() + DEADLINE, _proc.stdout.fileno()
    while b"\n" not in _buf:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise TimeoutError("sin respuesta en 5 s")
        chunk = os.read(fd, 65536)
        if not chunk:
            raise EOFError("el trabajador terminó")
        _buf += chunk
    line, _, _buf = _buf.partition(b"\n")
    return json.loads(line)


def _call(req: dict, acting: bool = False) -> dict:
    """Una petición. Lectura: un reintento con trabajador nuevo. Acción: nunca se repite."""
    with _lock:
        down = None
        if _proc is not None and _proc.poll() is not None:
            down = _down("el trabajador había muerto")
            if acting:
                raise down
        for _ in range(1 if acting else 2):
            if _proc is None:
                _spawn()
            try:
                _proc.stdin.write(json.dumps(req).encode() + b"\n")
            except OSError as e:
                down = _down(f"no aceptó la petición ({e})")
                continue
            try:
                resp = _read()
            except (TimeoutError, EOFError, ValueError) as e:
                down = _down(str(e))
                if acting:
                    raise ActionUncertain("La acción se envió y no hubo respuesta: puede haberse hecho. Haz "
                                          "ui_snapshot antes de repetirla.") from down
                continue
            err = resp.get("error")
            if err == "WorkerError":
                down = _down(resp["msg"])
                if acting:
                    raise ActionUncertain(f"La acción falló a medias ({resp['msg']}): puede haberse hecho. Haz "
                                          "ui_snapshot antes de repetirla.") from down
                continue
            if err == "A11yDown":
                raise _down(resp["msg"])
            if err:
                raise _ERRORS[err](resp["msg"])
            return resp
    raise down


# --- lo que llama el servidor -------------------------------------------------------------------------

def _line(i: int, r: dict, origin) -> str:
    s = f"[{i}] {r['role']} {json.dumps(r['name'], ensure_ascii=False)}"
    if "val" in r:
        s += f" {r['val']}"
    if r["actions"]:
        s += f" ({', '.join(r['actions'])})"
    if r["states"]:
        s += " {" + ", ".join(r["states"]) + "}"
    if origin and r.get("rect"):
        x, y, w, h = r["rect"]
        s += f" @{origin[0] + x:g},{origin[1] + y:g},{w},{h}"
    return s


def snapshot(window_uuid: str, find: str = "", max_nodes: int = 150, coords: bool = False) -> dict:
    """`ui_snapshot`: el listado de una ventana, con un ref por fila (refs.py, ámbito `atspi:<uuid>`)."""
    st = kwin.state()
    w = next((w for w in st["windows"] if w["uuid"] == window_uuid), None)
    if w is None:
        raise kwin.KWinError(f"KWin ya no tiene la ventana {window_uuid}; mira desktop_state.")
    if coords:
        spaces.output_at(st["outputs"], *w["client"][:2])  # escala ≠ 1 → OffScreen
    if not healthy():
        raise _down("el registro no contesta")
    try:
        fresh = enable_once()
    except dbus.DBusException as e:
        raise _down(f"no se pudo leer o poner IsEnabled ({e})") from e
    req = {"op": "snapshot", "win": window_uuid, "pid": w["pid"], "caption": w["caption"] or "",
           "size": [round(w["client"][2]), round(w["client"][3])], "find": find, "max": max_nodes, "coords": coords}
    deadline = time.monotonic() + (6 if fresh else 0)  # recién puesto IsEnabled, las apps tardan en entrar
    while True:
        try:
            r = _call(req)
            break
        except (NotInA11y, NoToplevel):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.3)
    # La generación es la del trabajador que contestó: un respawn en otro hilo no le cuela refs nuevos a handles
    # viejos. El ámbito entra en _scopes antes de registrar, para que un _down de en medio también lo olvide.
    scope = f"atspi:{window_uuid}"
    _scopes.add(scope)
    ids, _ = refs.register(scope, [refs.Ref(scope, f"{r['gen']}:{x['h']}", x["role"], x["name"]) for x in r["rows"]])
    origin = coords and w["client"][:2]
    return {"listing": [_line(i, x, origin) for i, x in zip(ids, r["rows"])], "nodes": r["nodes"],
            "shown": len(ids), "secrets": r["secrets"], "generation": r["gen"]}


def digest(window_uuid: str) -> str | None:
    """Huella del árbol AX de la ventana (más su título y geometría) para el detector de bucles. No acuña ni
    retira refs. None si no se puede leer: sin huella, el detector no rechaza nada. ~300-400 ms en kcalc.

    Nunca arranca un trabajador ni reintenta (semántica de acción): sin trabajador vivo, None. Un plazo vencido sí
    lo mata, como en cualquier lectura: tras una respuesta tardía la tubería quedaría desfasada."""
    w = next((w for w in kwin.state()["windows"] if w["uuid"] == window_uuid), None)
    if w is None or _proc is None or _proc.poll() is not None:
        return None
    try:
        rows = _keep(w, acting=True)
    except Exception:
        return None
    return hashlib.sha256(json.dumps([rows, w["caption"], w["client"]], sort_keys=True).encode()).hexdigest()


def _keep(w: dict, acting: bool) -> list[dict]:
    """Las filas de la ventana `w` de `kwin.state()` sin acuñar ni retirar refs."""
    return _call({"op": "snapshot", "keep": True, "win": w["uuid"], "pid": w["pid"], "caption": w["caption"] or "",
                  "size": [round(w["client"][2]), round(w["client"][3])]}, acting=acting)["rows"]


def census(w: dict) -> dict:
    """Cobertura AT-SPI de una ventana para memory.py: cuántos elementos y cuántos con acción. Solo recuentos."""
    rows = _keep(w, acting=False)
    return {"nodes": len(rows), "actions": sum(bool(r["actions"]) for r in rows)}


def act(ref: int, action: str, value: str = "") -> dict:
    """`ui_act`: press|focus|set_text por AT-SPI; type|key por EIS tras dar el foco al elemento."""
    r = refs.lookup(ref)
    if not r.frame_id.startswith("atspi:"):
        raise refs.StaleHandle(f"[{ref}] es del navegador, no del escritorio: usa las herramientas del navegador.")
    gen, h = map(int, r.xpath.split(":"))
    if gen != _gen:
        raise refs.StaleHandle(f"[{ref}] es de un trabajador AT-SPI anterior. Vuelve a ui_snapshot.")
    uuid, req, sent = r.frame_id[len("atspi:"):], {"op": "act", "h": h, "gen": gen, "role": r.role,
                                                    "name": r.name, "value": value}, None
    warnings = []
    try:
        if action in ("type", "key"):
            kwin.activate(uuid)
            out = _call({**req, "action": "focus"}, acting=True)
            deadline = time.monotonic() + FOCUS_WAIT  # sin el foco en ESTE campo, el texto iría a otro
            while "focused" not in out["after"]["states"]:
                if time.monotonic() >= deadline:
                    raise eis.FocusLost(0)
                time.sleep(0.03)
                out = _call({**req, "action": "state"})
            with eis.session(uuid) as kb:
                sent = kb.type(value) if action == "type" else kb.key(value)
            warnings = kb.warnings
            try:
                out = _call({**req, "action": "state"})
            except refs.StaleHandle:
                out = {"after": None}  # la tecla cerró su ventana (Intro en un diálogo): ya se envió
        else:
            out = _call({**req, "action": action}, acting=True)
    except refs.StaleHandle as e:
        raise refs.StaleHandle(f"[{ref}] {e} Vuelve a ui_snapshot.") from None
    return {"ref": ref, "action": action, "via": "eis" if action in ("type", "key") else "atspi",
            "verified": out.get("verified"), "sent": sent, "after": out["after"], "warnings": warnings}
