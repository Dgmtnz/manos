"""Peldaño 1 del escritorio: las QAction de una app KXmlGui por D-Bus, y lanzar apps con kstart.

Descubrimiento (medido en 6.8.2): el servicio es un nombre del bus cuyo dueño es el pid de la ventana (kcalc
solo tiene su nombre único `:1.N`; Dolphin además `org.kde.dolphin-<pid>`) y el objeto es el camino de
profundidad 2 con un hijo `/actions` (kcalc `/kcalc/MainWindow_1`, Dolphin `/dolphin/Dolphin_1`). Las
acciones de `KMainWindow.actions` que tienen nodo en `<objeto>/actions/<nombre>` dan texto y estado por
`org.qtproject.Qt.QAction`; las demás (las de ayuda, en kcalc 7 de 24), solo `actionIsEnabled`.

Solo llamadas al lado de la app, literales y con tipos básicos (ALLOWLIST de tests/test_desktop.py). Nunca a
kwin_wayland ni a plasmashell: un pid de `kwin.shell_pids()` da `Refused`.
Autocomprobación (solo en KWin anidada):  uv run --project . tests/test_apps_nest.py
"""

import configparser
import difflib
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import dbus
import dbus.bus

import kwin
from browser.session import check

DESTRUCTIVE = re.compile(r"delet|shred|empty_?trash|purge|discard|revert", re.I)  # D15
ACT_TIMEOUT = 2.0  # s: más es casi siempre un diálogo modal abierto por la acción
LAUNCH_TIMEOUT = 20.0
_APP = re.compile(r"\w[\w.+-]*")  # un desktop id; nunca algo que kstart lea como opción


class Choices(LookupError):
    """Error con alternativas: `available` va al modelo (≤10)."""

    def __init__(self, message: str, available=()):
        super().__init__(message)
        self.available = list(available)[:10]


class AmbiguousWindow(Choices): pass
class NotFound(Choices): pass
class NoSuchAction(Choices): pass
class NotInstalled(Choices): pass
class Refused(PermissionError): pass
class NotKXmlGui(LookupError): pass
class ActionDisabled(RuntimeError): pass
class NeedsApproval(PermissionError): pass
class LaunchTimeout(TimeoutError): pass
class SensitiveWindowOpen(PermissionError): pass


def _conn() -> dbus.bus.BusConnection:
    return dbus.bus.BusConnection(kwin.bus_address())


def _children(conn, service: str, path: str) -> list[str]:
    xml = conn.call_blocking(service, path, "org.freedesktop.DBus.Introspectable", "Introspect", "", (),
                             timeout=ACT_TIMEOUT)
    return [n.get("name") for n in ET.fromstring(str(xml)).findall("node")]


def _find(conn, window: dict) -> tuple[str, str]:
    """(servicio, objeto KXmlGui) de la ventana `{pid, caption}` de `kwin.state()`."""
    pid = window["pid"]
    if pid in kwin.shell_pids():
        raise Refused("Esa ventana es de kwin_wayland o plasmashell: manos no les manda acciones.")
    names = conn.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "ListNames", "", ())
    for name in sorted(map(str, names), key=lambda n: n.startswith(":")):  # los conocidos primero
        try:
            if conn.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "GetConnectionUnixProcessID", "s", (name,)) != pid:
                continue
            objs = [f"/{a}/{b}" for a in _children(conn, name, "/") for b in _children(conn, name, f"/{a}")
                    if "actions" in _children(conn, name, f"/{a}/{b}")]
        except dbus.DBusException:
            continue  # el nombre se fue mientras se miraba, o no es introspectable
        if len(objs) > 1:  # varias ventanas en un proceso: la del título que KWin muestra
            cap = window.get("caption") or ""
            hit = [o for o in objs if (t := str(conn.call_blocking(
                name, o, "org.freedesktop.DBus.Properties", "Get", "ss", ("org.qtproject.Qt.QWidget", "windowTitle"),
                timeout=ACT_TIMEOUT))) and t in cap]
            if len(hit) != 1:
                raise AmbiguousWindow(f"El proceso {pid} tiene {len(objs)} ventanas KXmlGui y el título no decide.")
            objs = hit
        if objs:
            return name, objs[0]
    raise NotKXmlGui(f"La ventana (pid {pid}) no expone acciones KXmlGui por D-Bus.")


def _action(conn, service: str, obj: str, name: str, nodes: set) -> dict:
    if name not in nodes:  # sin nodo propio: solo se sabe si está activa
        return {"name": name, "text": "", "checkable": None, "checked": None,
                "enabled": bool(conn.call_blocking(service, obj, "org.kde.KMainWindow", "actionIsEnabled", "s",
                                                   (name,), timeout=ACT_TIMEOUT))}
    p = conn.call_blocking(service, f"{obj}/actions/{name}", "org.freedesktop.DBus.Properties", "GetAll", "s",
                           ("org.qtproject.Qt.QAction",), timeout=ACT_TIMEOUT)
    return {"name": name, "text": str(p.get("text", "")).replace("&", ""), "enabled": bool(p.get("enabled")),
            "checkable": bool(p.get("checkable")), "checked": bool(p.get("checked"))}


def _names(conn, service: str, obj: str) -> list[str]:
    return [str(n) for n in conn.call_blocking(service, obj, "org.kde.KMainWindow", "actions", "", (),
                                               timeout=ACT_TIMEOUT)]


def actions(window: dict, find: str = "", limit: int = 60) -> dict:
    """`{service, object, actions[{name,text,enabled,checkable,checked}], total}`; `find` filtra por nombre o texto."""
    conn = _conn()
    try:
        service, obj = _find(conn, window)
        nodes = set(_children(conn, service, obj + "/actions"))
        rows = [_action(conn, service, obj, n, nodes) for n in _names(conn, service, obj)]
        hits = [r for r in rows if find.lower() in f"{r['name']} {r['text']}".lower()]
        return {"service": service, "object": obj, "actions": hits[:limit], "total": len(hits)}
    finally:
        conn.close()


def invoke(window: dict, action: str, approved: bool = False) -> dict:
    """Dispara una acción por su objectName: `{invoked, before{enabled,checked}, after{…}}`.

    `invoked` es None si la app no contestó en ACT_TIMEOUT (casi siempre: abrió un diálogo modal)."""
    if DESTRUCTIVE.search(action) and not approved:
        raise NeedsApproval(f"'{action}' puede borrar o descartar algo.")
    conn = _conn()
    try:
        service, obj = _find(conn, window)
        names = _names(conn, service, obj)
        if action not in names:
            raise NoSuchAction(f"No hay ninguna acción '{action}'.", difflib.get_close_matches(action, names, 10, 0.4))
        nodes = set(_children(conn, service, obj + "/actions"))
        state = lambda: {k: v for k, v in _action(conn, service, obj, action, nodes).items() if k in ("enabled", "checked")}
        before = state()
        if not conn.call_blocking(service, obj, "org.kde.KMainWindow", "actionIsEnabled", "s", (action,),
                                  timeout=ACT_TIMEOUT):  # releído justo antes de disparar
            raise ActionDisabled(f"La acción '{action}' está desactivada ahora mismo.")
        try:
            invoked = bool(conn.call_blocking(service, obj, "org.kde.KMainWindow", "activateAction", "s", (action,),
                                              timeout=ACT_TIMEOUT))
        except dbus.DBusException as e:
            if e.get_dbus_name() != "org.freedesktop.DBus.Error.NoReply":
                raise
            return {"invoked": None, "before": before, "after": None}
        try:
            after = state()
        except dbus.DBusException:
            after = None  # la acción cerró la ventana o la app (file_quit): se hizo, no hay después
        return {"invoked": invoked, "before": before, "after": after}
    finally:
        conn.close()


def _desktop_ids() -> dict[str, Path]:
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    dirs = [home, *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    return {p.stem: p for d in reversed(dirs) for p in Path(d, "applications").glob("*.desktop")}


def launch(app: str, url: str = "", target: str = "front", timeout: float = LAUNCH_TIMEOUT) -> dict:
    """`kstart --application <app> [--url u]` y espera su ventana: `{window, pid, ms}`.

    La ventana nueva es la primera que aparece con el resourceClass de la app: el desktop id o su
    StartupWMClass (kwrite declara `kwrite` y en Wayland es `org.kde.kwrite`). `parked` la minimiza y la saca
    de la barra al nacer (`kwin.load_tamer`, uno por clase); en cuanto se ve vuelve a la barra y a las capturas y
    se queda solo minimizada, y los domadores se descargan: las ventanas siguientes de esa clase no se le
    esconden a Diego.
    """
    ids = _desktop_ids()
    if not _APP.fullmatch(app) or app not in ids:
        raise NotInstalled(f"No hay ninguna aplicación instalada con id '{app}'.",
                           difflib.get_close_matches(app, ids, 10, 0.4))
    entry = configparser.ConfigParser(interpolation=None, strict=False)
    entry.read(ids[app], encoding="utf-8")
    classes = list(dict.fromkeys([app, entry.get("Desktop Entry", "StartupWMClass", fallback=app)]))
    t0 = time.monotonic()
    seen = {w["uuid"] for w in kwin.state()["windows"]}
    tamers = [kwin.load_tamer(c, f"manoslaunch{i}") for i, c in enumerate(classes)] if target == "parked" else []
    try:
        subprocess.Popen(["kstart", "--application", app, *(["--url", url] if url else [])],
                         env={**os.environ, "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1"}, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while time.monotonic() - t0 < timeout:
            check()
            new = [w for w in kwin.state()["windows"] if w["uuid"] not in seen and w["resource_class"] in classes]
            if new:
                if target == "front":
                    kwin.activate(new[0]["uuid"])
                else:  # el domador la sacó de la barra y de las capturas; aparcada es solo minimizada
                    kwin.window_set(new[0]["uuid"], skipTaskbar=False, skipSwitcher=False, skipPager=False,
                                    excludeFromCapture=False)
                return {"window": new[0]["uuid"], "pid": new[0]["pid"], "resource_class": new[0]["resource_class"],
                        "ms": round((time.monotonic() - t0) * 1000)}
            time.sleep(0.2)
    finally:
        for i, path in enumerate(tamers):
            kwin.unload(f"manoslaunch{i}")
            os.unlink(path)
    raise LaunchTimeout(f"Ninguna ventana nueva de '{app}' en {timeout:.0f} s.")
