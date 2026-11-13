"""Teclado y ratón por el EIS de KWin (libei por ctypes), una sesión por llamada: `with session(uuid) as kb`.

Frenos (docs/p3-design.md §5): una sola sesión a la vez (cerrojo), parada, bloqueo, modificador de Diego y,
antes de cada bloque de ≤8 pulsaciones, ABORT, parada, bloqueo y foco. Intro y Tab van solos en su bloque.
El D-Bus de EIS es del guardián (`kwin.eis_open`); aquí solo está el socket EIS.
techo: libei corre dentro del servidor; si algún día da un segfault, pasarlo a un subproceso.
Autocomprobación (solo en KWin anidada):  uv run --project . tests/test_desktop_nest.py
"""

import contextlib
import ctypes
import fcntl
import mmap
import os
import select
import subprocess
import threading
import time
from functools import cache
from pathlib import Path

import kwin
from browser import session as _browser
from desktop import xkb_es
from desktop.xkb_es import InputUnavailable

ABORT = threading.Event()  # lo pone el servidor al cancelar; lo limpia quien lo pone, antes de empezar
CHUNK = 8


class ScreenLocked(RuntimeError): pass
class ModifierHeld(RuntimeError): pass


class _Cut(RuntimeError):
    def __init__(self, sent):
        super().__init__(f"{self.__doc__}; se enviaron {sent} caracteres.")
        self.sent = sent


class FocusLost(_Cut):
    """La ventana perdió el foco (no reintentes sin mirar)"""


class Aborted(_Cut):
    """Cancelado"""


# de kwin-mcp input.py:304-341 (MIT), solo lo usado; KEYBOARD_MODIFIERS añadido
_CAP_POINTER_ABSOLUTE, _CAP_KEYBOARD, _CAP_BUTTON = 1 << 1, 1 << 2, 1 << 5
_EV_DISCONNECT, _EV_SEAT_ADDED, _EV_DEVICE_ADDED = 2, 3, 5
_EV_DEVICE_REMOVED, _EV_DEVICE_PAUSED, _EV_DEVICE_RESUMED, _EV_MODIFIERS = 6, 7, 8, 9
_EV_PONG = 90


@cache
def _lib():
    # de kwin-mcp input.py:406-534 (MIT), sin táctil ni puntero relativo; más keymap, modificadores y grupo
    lib = ctypes.CDLL("libei.so.1")
    p, i, u32, u64, d = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_double
    for name, res, args in [
        ("ei_new_sender", p, [p]), ("ei_configure_name", None, [p, ctypes.c_char_p]),
        ("ei_setup_backend_fd", i, [p, i]), ("ei_get_fd", i, [p]), ("ei_unref", p, [p]),
        ("ei_dispatch", None, [p]),  # void: con restype int salía basura negativa (upstream)
        ("ei_get_event", p, [p]), ("ei_event_get_type", i, [p]), ("ei_event_unref", p, [p]),
        ("ei_new_ping", p, [p]), ("ei_ping", None, [p]), ("ei_ping_get_id", u64, [p]),
        ("ei_ping_unref", p, [p]), ("ei_event_pong_get_ping", p, [p]), ("ei_event_get_seat", p, [p]),
        ("ei_seat_has_capability", i, [p, ctypes.c_uint]), ("ei_seat_bind_capabilities", None, [p]),  # variádica
        ("ei_event_get_device", p, [p]), ("ei_device_has_capability", i, [p, ctypes.c_uint]),
        ("ei_device_ref", p, [p]), ("ei_device_unref", p, [p]), ("ei_device_frame", None, [p, u64]),
        ("ei_device_pointer_motion_absolute", None, [p, d, d]),
        ("ei_device_keyboard_key", None, [p, u32, i]), ("ei_device_stop_emulating", None, [p]),
        ("ei_device_start_emulating", None, [p, u32]), ("ei_device_keyboard_get_keymap", p, [p]),
        ("ei_keymap_get_type", i, [p]), ("ei_keymap_get_fd", i, [p]), ("ei_keymap_get_size", ctypes.c_size_t, [p]),
        ("ei_event_keyboard_get_xkb_mods_depressed", u32, [p]), ("ei_event_keyboard_get_xkb_mods_locked", u32, [p]),
        ("ei_event_keyboard_get_xkb_group", u32, [p]),
    ]:
        f = getattr(lib, name)
        f.restype, f.argtypes = res, args
    return lib


def locked() -> bool:
    """LockedHint de logind. Solo en la sesión en vivo; ante cualquier duda, bloqueada."""
    if kwin.BUS != "--user":
        return False
    r = subprocess.run(["loginctl", "show-session", os.environ.get("XDG_SESSION_ID", "auto"),
                        "-p", "LockedHint", "--value"], capture_output=True, text=True)
    return r.stdout.strip() != "no"


class _Session:
    def __init__(self, target):
        self.target, self.warnings = target, []
        self.ei = self.kbd = self.ptr = self.btn = 0
        self.emulating, self.pongs, self.dead = set(), set(), False
        self.depressed = self.lock_mods = self.group = self.mod_events = 0
        self.pressed = []  # códigos abajo, en orden: al cerrar se sueltan y deciden si el cierre es limpio

    # --- libei: de kwin-mcp input.py:578-748 (MIT), `_setup` recibe el fd y no hay D-Bus aquí ---------

    def _setup(self, fd, timeout=5.0):
        lib = _lib()
        self.ei = lib.ei_new_sender(None)
        if not self.ei:
            os.close(fd)
            raise InputUnavailable("libei no creó el contexto")
        lib.ei_configure_name(self.ei, b"manos")
        if lib.ei_setup_backend_fd(self.ei, fd) != 0:  # libei se queda el fd aunque falle
            raise InputUnavailable("ei_setup_backend_fd falló")
        devs = lambda: {self.kbd, self.ptr, self.btn}
        start = time.monotonic()
        while time.monotonic() - start < timeout and not (0 not in devs() and devs() <= self.emulating):
            self._drain(0.3)
            if self.dead:
                raise InputUnavailable("KWin cerró el EIS durante el saludo")
        if 0 in devs():
            raise InputUnavailable("KWin no ofreció teclado, puntero absoluto y botones por EIS")
        for dev in devs() - self.emulating:  # nunca reanudados: se arrancan igual (upstream)
            lib.ei_device_start_emulating(dev, 0)
            self.emulating.add(dev)

    def _handle(self, event):
        lib = _lib()
        try:
            t = lib.ei_event_get_type(event)
            if t == _EV_DISCONNECT:
                self.dead = True
            elif t == _EV_SEAT_ADDED:  # de kwin-mcp input.py:858-945 (MIT)
                seat = lib.ei_event_get_seat(event)
                caps = [c for c in (_CAP_POINTER_ABSOLUTE, _CAP_KEYBOARD, _CAP_BUTTON) if lib.ei_seat_has_capability(seat, c)]
                lib.ei_seat_bind_capabilities(seat, *[ctypes.c_uint(c) for c in caps], ctypes.c_void_p(None))
            elif t == _EV_DEVICE_ADDED:
                dev = lib.ei_event_get_device(event)
                for attr, cap in (("ptr", _CAP_POINTER_ABSOLUTE), ("kbd", _CAP_KEYBOARD), ("btn", _CAP_BUTTON)):
                    if lib.ei_device_has_capability(dev, cap) and not getattr(self, attr):
                        setattr(self, attr, lib.ei_device_ref(dev))
            elif t in (_EV_DEVICE_REMOVED, _EV_DEVICE_PAUSED, _EV_DEVICE_RESUMED):
                dev = lib.ei_event_get_device(event)
                if dev in (self.kbd, self.ptr, self.btn):
                    if t == _EV_DEVICE_RESUMED and dev not in self.emulating:
                        lib.ei_device_start_emulating(dev, 0)
                        self.emulating.add(dev)
                    elif t != _EV_DEVICE_RESUMED:  # cambio de distribución o de salida: la sesión ya no vale
                        self.dead = True
                        self.emulating.discard(dev)
            elif t == _EV_MODIFIERS:
                self.mod_events += 1
                self.depressed = lib.ei_event_keyboard_get_xkb_mods_depressed(event)
                self.lock_mods = lib.ei_event_keyboard_get_xkb_mods_locked(event)
                self.group = lib.ei_event_keyboard_get_xkb_group(event)
            elif t == _EV_PONG and (ping := lib.ei_event_pong_get_ping(event)):
                self.pongs.add(lib.ei_ping_get_id(ping))
        finally:
            lib.ei_event_unref(event)

    def sync(self, timeout=1.0):
        """Ida y vuelta ping/pong: KWin ya procesó todo lo enviado y los modificadores están al día."""
        lib = _lib()
        if not self.ei or self.dead or not (ping := lib.ei_new_ping(self.ei)):
            return
        try:
            pid = lib.ei_ping_get_id(ping)
            lib.ei_ping(ping)
            lib.ei_dispatch(self.ei)
            deadline = time.monotonic() + timeout
            while pid not in self.pongs and not self.dead and (left := deadline - time.monotonic()) > 0:
                self._drain(left)
            self.pongs.discard(pid)
        finally:
            lib.ei_ping_unref(ping)

    def _drain(self, timeout):
        # de kwin-mcp input.py:783-808 (MIT)
        lib = _lib()
        fd, wait = lib.ei_get_fd(self.ei), max(0.0, timeout)
        for _ in range(64):  # tope de rondas: un socket que no para no gira para siempre
            while event := lib.ei_get_event(self.ei):
                self._handle(event)
            if self.dead or not select.select([fd], [], [], wait)[0]:
                return
            lib.ei_dispatch(self.ei)
            wait = 0.0

    def close(self):
        # de kwin-mcp input.py:994-1041 (MIT), sin táctil
        if not self.ei:
            return
        lib = _lib()
        with contextlib.suppress(OSError, ValueError):
            self._drain(0.0)
        if not self.dead:
            for dev in {self.kbd, self.ptr, self.btn} & self.emulating:
                lib.ei_device_stop_emulating(dev)
        for attr in ("kbd", "ptr", "btn"):  # una referencia por ranura, aunque compartan dispositivo
            if dev := getattr(self, attr):
                lib.ei_device_unref(dev)
                setattr(self, attr, 0)
        lib.ei_unref(self.ei)  # cierra el socket EIS
        self.ei = 0

    # --- envío ----------------------------------------------------------------------------------------

    def _emit(self, dev, call, *args):
        self._drain(0.0)
        if self.dead:
            raise InputUnavailable("KWin quitó o pausó el dispositivo EIS (¿cambio de distribución o de salida?)")
        call(dev, *args)
        _lib().ei_device_frame(dev, int(time.monotonic() * 1_000_000))
        _lib().ei_dispatch(self.ei)

    def _send(self, code, state):
        if state and code not in self.pressed:
            self.pressed.append(code)
        self._emit(self.kbd, _lib().ei_device_keyboard_key, code, state)
        if not state and code in self.pressed:
            self.pressed.remove(code)

    def _keymap(self):
        lib = _lib()
        km = lib.ei_device_keyboard_get_keymap(self.kbd)
        if not km or lib.ei_keymap_get_type(km) != 1:  # 1 = EI_KEYMAP_TYPE_XKB
            raise InputUnavailable("el teclado EIS no trae keymap XKB")
        with mmap.mmap(lib.ei_keymap_get_fd(km), lib.ei_keymap_get_size(km), mmap.MAP_PRIVATE, mmap.PROT_READ) as m:
            return xkb_es.Keymap(m[:].split(b"\0", 1)[0].decode())

    # --- lo que se usa desde fuera -----------------------------------------------------------------

    def _guard(self, sent=0, focus=True):
        if ABORT.is_set():
            raise Aborted(sent)
        _browser.check()
        if locked():
            raise ScreenLocked("La pantalla está bloqueada.")
        if focus and kwin.active() != self.target:  # lo último, justo antes de enviar
            raise FocusLost(sent)

    def _stroke(self, mods, code):
        # ritmo de upstream: 10 ms tras cada modificador y la tecla, 20 ms entre pulsaciones
        for m in mods:
            self._send(m, 1)
            time.sleep(0.01)
        self._send(code, 1)
        time.sleep(0.01)
        self._send(code, 0)
        for m in reversed(mods):
            time.sleep(0.01)
            self._send(m, 0)
        time.sleep(0.02)

    def type(self, text):
        """Escribe `text` en la ventana objetivo. Devuelve los caracteres enviados."""
        self.sync()  # bloqueos (CapsLock) y grupo al día
        km = self.keymap
        km.plan(text, self.lock_mods, self.group)  # Untypeable con todos los caracteres, antes de enviar nada
        chunks = []
        for ch in text:
            n = len(km.plan(ch, self.lock_mods, self.group))
            if not chunks or ch in "\n\t" or chunks[-1][0] in "\n\t" or chunks[-1][1] + n > CHUNK:
                chunks.append(["", 0])
            chunks[-1][0] += ch
            chunks[-1][1] += n
        sent = 0
        for chars, _ in chunks:
            self.sync()  # CapsLock o grupo pueden cambiar a mitad: cada bloque se planifica al enviarlo
            strokes = km.plan(chars, self.lock_mods, self.group)
            self._guard(sent)
            for mods, code in strokes:
                self._stroke(mods, code)
            sent += len(chars)
        return sent

    def key(self, chord):
        """Un atajo (`ctrl+s`, `return`, `shift+tab`). Los de Meta y los de VT, denegados."""
        mods, code = self.keymap.chord(chord)
        self._guard()
        self._stroke(mods, code)

    def move(self, x, y):
        """Puntero a (x, y) en coordenadas lógicas del escritorio."""
        self._guard(focus=False)
        self._emit(self.ptr, _lib().ei_device_pointer_motion_absolute, float(x), float(y))

    def _finish(self):
        if not self.ei or self.dead:
            return  # sin dispositivo no hay suelta posible: KWin las suelta al destruir el contexto
        for code in reversed(self.pressed[:]):
            self._send(code, 0)
        self.sync()
        if self.depressed:  # otro dispositivo (Diego): no se puede soltar desde aquí, solo avisar
            self.warnings.append(f"Queda pulsado un modificador que no es nuestro (máscara {self.depressed:#x}).")


def _connect():
    """(fd EIS, enlace, guardián) bajo el cerrojo de sesión única."""
    _lib()  # una libei ausente falla antes de arrancar el guardián
    # Una sola sesión a la vez en el usuario: dos contextos EIS vivos son el techo (3) de kwin.py. El cerrojo
    # pasa al guardián y dura hasta que este haya desconectado y salido.
    run = Path(os.environ["XDG_RUNTIME_DIR"]) / "manos"
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(run / "eis.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InputUnavailable("Otra sesión de entrada en curso; reintenta cuando acabe.") from None
        try:
            return kwin.eis_open(keep_fds=[lock])
        except Exception as e:
            raise InputUnavailable(str(e)) from e
    finally:
        os.close(lock)


def probe() -> xkb_es.Keymap:
    """`--diag --live-eis`: saludo EIS y keymap XKB sin enviar un solo evento; desconecta limpio."""
    fd, link, guard = _connect()
    s = _Session(None)
    try:
        s._setup(fd)
        return s._keymap()
    finally:
        s.close()
        kwin.eis_close(link, guard, clean=True)


@contextlib.contextmanager
def session(target_uuid):
    """Abre EIS, comprueba los frenos y entrega `type/key/move`. Todo se suelta al salir."""
    _browser.check()
    if locked():
        raise ScreenLocked("La pantalla está bloqueada.")
    fd, link, guard = _connect()
    s = _Session(target_uuid)
    s.guard = guard
    try:
        s._setup(fd)
        # KWin no manda el estado al conectar y solo avisa cuando cambia (medido): Shift derecho lo provoca, y
        # si Shift ya estaba abajo no cambia nada y se prueba Ctrl derecho. Sin ningún aviso, ambos abajo.
        for code in (54, 97):
            s._send(code, 1)
            time.sleep(0.01)
            s._send(code, 0)
            s.sync()
            if s.mod_events:
                break
        if s.depressed or not s.mod_events:  # Diego tiene una tecla pulsada: `w` sería Ctrl+W
            raise ModifierHeld(f"Hay un modificador pulsado (máscara {s.depressed or 5:#x}).")
        s.keymap = s._keymap()
        yield s
    finally:
        try:
            with contextlib.suppress(InputUnavailable):
                s._finish()
        finally:
            s.close()
            if not kwin.eis_close(link, guard, clean=not s.pressed):
                s.warnings.append(f"El guardián EIS (pid {guard.pid}) no salió a tiempo.")
