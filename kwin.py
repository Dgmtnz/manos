"""El canal hacia KWin: un script efímero por llamada, y la respuesta de vuelta por el diario.

KWin solo contesta a un script cargado a través de una callback D-Bus que exige iterar un contexto
principal de GLib, y eso no puede pasar dentro del proceso asyncio del servidor. La salida es que el
script escriba su respuesta con `print()` —que aterriza en el diario del usuario— y que aquí se lea de
ahí. Cuesta un `journalctl` por sondeo y ahorra el demonio receptor entero.

El precio real de cada llamada, para no fingir que es barata: `run()` dispara **una sola vez** por
objeto de script, así que toda consulta es unloadScript + loadScript + run, tres viajes D-Bus y un
fichero temporal. Por eso una lectura simple de ventanas NO pasa por aquí: `windows()` usa
`/WindowsRunner` + `/KWin.getWindowInfo`, que no necesita script ninguno.

Lo medido en esta máquina, y que este módulo da por cierto:
  - Escribir solo la posición de `frameGeometry` es **síncrono** y se relee bien en el mismo tick.
  - Cambiar el **tamaño** no lo es, y la medida del 2026-10-30 («asienta en 1-2 ms») estaba mal: el
    2026-10-31, diez corridas, un cambio real de tamaño se **traga la primera escritura** 3 de 6 veces
    y no llega ni esperando 5 s; una **segunda** escritura lo asienta en ~75 ms, 10 de 10 (~317 ms de
    punta a punta). Por eso `window_set` escribe, espera, relee y vuelve a pedir hasta `GEO_SETTLE`.
  - Un cambio de tamaño **sí pega** aunque la ventana esté fuera de toda salida (x=3400 sobre un
    espacio de 3000 de ancho, y la ventana más ancha que la salida a la que KWin la asigna), y KWin lo
    aplica **con su propio clamp**: una ventana de Chrome no baja de ~500 px de ancho y no hay ni señal
    ni error que lo diga. Por eso la única comprobación válida es relectura contra lo pedido, y no hay
    heurística de rechazo: la que había deducía de un `QTimer` de 60 ms y llegó a informar de un rechazo
    que no hubo, con una geometría que ya no era cierta.
  - `frameGeometry` es un `RectF`: viene en float y tratarlo como int lo trunca en silencio.
  - `Match("")` de WindowsRunner devuelve cada ventana DOS veces, porque empareja título y nombre de app.

Autocomprobación:  uv run --project . tests/test_session.py
"""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid as _uuid

# Props de ventana que se pueden escribir desde aquí. Es una lista blanca, no un adorno: el nombre se
# interpola en el fuente JS que se le pasa a KWin.
# `tag` NO está: KWin 6.7.4 no la expone y el try/catch del script la reporta en `failed` (medido).
WRITABLE = frozenset({"minimized", "skipTaskbar", "skipSwitcher", "skipPager", "excludeFromCapture"})

GEO_SETTLE = 1.5  # segundos que `window_set` espera (y reescribe) hasta que la geometría pedida pega

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class KWinError(RuntimeError):
    """KWin no pudo hacer lo que se le pidió; el mensaje es para el modelo."""


# A qué KWin se habla: "--user" es la sesión en vivo. tests/nest.py lo cambia por el "--address=" de
# una KWin anidada mientras dura su bloque, y `_JOURNAL` por la lectura del stderr de esa KWin.
# MANOS_KWIN_BUS/MANOS_KWIN_LOG hacen lo mismo para un proceso hijo (el servidor que lanza el banco).
BUS = f"--address={os.environ['MANOS_KWIN_BUS']}" if os.environ.get("MANOS_KWIN_BUS") else "--user"


def bus_address() -> str:
    """La dirección del bus al que apunta `BUS`: la de la anidada o la de la sesión en vivo."""
    if BUS != "--user":
        return BUS.removeprefix("--address=")
    return os.environ.get("DBUS_SESSION_BUS_ADDRESS") or \
        f"unix:path={os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'}/bus"


def _busctl(*args, check=True):
    r = subprocess.run(["busctl", BUS, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise KWinError(f"busctl {' '.join(args[:4])}: {r.stderr.strip()}")
    return r.stdout.strip()


# --- lectura: sin script, tres órdenes de magnitud más barata -------------------------------------

def windows(resource_class: str | None = None) -> list[dict]:
    """Las ventanas que KWin conoce, opcionalmente filtradas por `resourceClass`. No carga ningún script.

    El filtro se le pasa a `Match`, que lo resuelve del lado de KWin: medido, `Match("ManosBrowser")`
    tarda 11 ms y devuelve una ventana, mientras que enumerarlas todas y preguntar por cada una cuesta
    271 ms y 38 llamadas a `getWindowInfo`. `Match` es difuso, así que el filtro exacto se repite aquí.
    """
    out = _busctl("call", "org.kde.KWin", "/WindowsRunner", "org.kde.krunner1",
                  "Match", "s", resource_class or "")
    seen, found = set(), []
    for u in re.findall(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", out):
        if u in seen:  # Match("") empareja caption Y nombre de app: cada ventana sale dos veces
            continue
        seen.add(u)
        info = window_info(u)
        if info and (resource_class is None or info.get("resourceClass") == resource_class):
            found.append(info)
    return found


def window_info(window_uuid: str) -> dict | None:
    """Los datos de una ventana por su uuid, o None si ya no existe."""
    if not _UUID.match(window_uuid):
        raise ValueError(f"uuid de ventana con forma inesperada: {window_uuid!r}")
    out = _busctl("--json=short", "call", "org.kde.KWin", "/KWin", "org.kde.KWin",
                  "getWindowInfo", "s", window_uuid, check=False)
    if not out:
        return None
    data = json.loads(out)["data"][0]
    if not data:
        return None
    info = {k: v["data"] for k, v in data.items()}
    info["uuid"] = window_uuid
    return info


def wait_window(resource_class: str, timeout: float = 20.0) -> dict:
    """Bloquea hasta que KWin vea una ventana de esa clase. La ventana se mapea un instante después de
    que el navegador conteste por CDP, así que hay que esperarla."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for info in windows(resource_class):
            return info
        time.sleep(0.1)
    raise KWinError(f"KWin nunca vio una ventana con resourceClass='{resource_class}' "
                    f"en {timeout:.0f} s. ¿Arrancó el navegador con --class?")


# --- escritura: un script efímero por llamada -----------------------------------------------------

def load(path: str, name: str) -> str:
    """Carga un script y lo ejecuta. `run` dispara una sola vez por objeto de script."""
    out = _busctl("call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                  "loadScript", "ss", path, name)
    sid = out.split()[-1]
    _busctl("call", "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run")
    return sid


def unload(name: str) -> None:
    _busctl("call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
            "unloadScript", "s", name, check=False)


def is_loaded(name: str) -> bool:
    return "true" in _busctl("call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                             "isScriptLoaded", "s", name, check=False)


_JOURNAL = ["journalctl", "--user", "_SYSTEMD_USER_UNIT=plasma-kwin_wayland.service",
            "-o", "cat", "-n", "2000", "--no-pager"]
if os.environ.get("MANOS_KWIN_LOG"):
    _JOURNAL = ["tail", "-n", "2000", os.environ["MANOS_KWIN_LOG"]]
# The poll is filtered to KWin's own unit and the window raised to 2000 entries, because the user
# journal is a SHARED channel: ~436 ambient lines a minute, and the browser itself writes there (it
# runs in a systemd scope). An unrelated burst of 200 lines inside the 100 ms poll interval used to
# push the reply out of an unfiltered `-n 200` window, and run_js then raised with no way to tell a
# lost reply from a script that never answered. Measured on this machine: the whole filtered history
# is 428 lines, so the reply now outlives any noise KWin can make on its own, and the read is 5.9 ms
# instead of 11.0. journald still drops on RateLimitBurst, so a timeout is not proof of a failure.
#
# PRIVACY CEILING: whatever goes through REPLY is written to the persistent system journal, which
# nothing here sanitises. Geometry only. A window title or any page content put through this channel
# would be a secret in a log forever; it has to stay out.


# G5: una respuesta solo lleva números, booleanos e identificadores (uuid, resourceClass, salida). Un
# texto libre (un título, un valor) en el diario es un secreto en un log para siempre: se rechaza.
_LEAF = re.compile(r"[\w.{}:-]{0,64}", re.ASCII)
_FORBIDDEN_JS = re.compile(r"registerShortcut|callDBus|kglobalaccel")  # la clase del cuelgue de la sesión


def _leaves_ok(o) -> bool:
    if isinstance(o, str):
        return bool(_LEAF.fullmatch(o))
    if isinstance(o, dict):
        return all(_leaves_ok(k) and _leaves_ok(v) for k, v in o.items())
    if isinstance(o, list):
        return all(map(_leaves_ok, o))
    return True


def _read_reply(marker: str, deadline: float):
    while True:
        out = subprocess.run(_JOURNAL, capture_output=True, text=True).stdout
        for line in reversed(out.splitlines()):
            if marker in line:
                reply = json.loads(line.split(marker, 1)[1].strip())
                if not _leaves_ok(reply):
                    raise KWinError("La respuesta de KWin trae texto libre; solo se admiten identificadores (G5).")
                return reply
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)  # en vivo cada sondeo es un journalctl: no bajar de aquí


def run_js(body: str, timeout: float = 8.0) -> dict:
    """Ejecuta un script en KWin y devuelve lo que pase a `REPLY(obj)`. El script se descarga siempre."""
    if _FORBIDDEN_JS.search(body):
        raise KWinError("Script rechazado: registerShortcut, callDBus y kglobalaccel no se usan desde manos.")
    tag = "manos" + _uuid.uuid4().hex[:8]
    marker = "MANOS_" + tag
    js = f"function REPLY(o){{print('{marker} ' + JSON.stringify(o));}}\n" + body
    fd, path = tempfile.mkstemp(suffix=".js", prefix=tag)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(js)
        load(path, tag)
        reply = _read_reply(marker, time.monotonic() + timeout)
    finally:
        unload(tag)
        os.unlink(path)
    if reply is None:
        raise KWinError(f"KWin no contestó al script {tag} en {timeout:.0f} s.")
    return reply


_FIND = """
var W = null, L = workspace.windowList();
for (var i = 0; i < L.length; i++)
    if (String(L[i].internalId).replace(/[{}]/g, "") == %s) { W = L[i]; break; }
"""


def window_set(window_uuid: str, geometry: tuple[float, float, float, float] | None = None,
               timeout: float = 8.0, **props) -> dict:
    """Escribe geometría y/o props de una ventana y devuelve lo que KWin tiene **de verdad** después.

    `geometry` es (x, y, ancho, alto) en coordenadas lógicas, en float. Devuelve
    `{"geometry", "before", "failed", "minimized"}`, donde `geometry` es una **relectura** posterior a
    la escritura —no lo que un temporizador dentro del script alcanzó a ver— y `failed` nombra las
    props que esta versión de KWin no supo escribir.

    Comparar lo pedido con `geometry` es la única forma fiable de saber qué aplicó KWin: aplica con su
    propio clamp (una ventana de Chrome no baja de ~500 px de ancho) y no lo avisa de ninguna manera.
    """
    if not _UUID.match(window_uuid):
        raise ValueError(f"uuid de ventana con forma inesperada: {window_uuid!r}")
    bad = set(props) - WRITABLE
    if bad:
        raise ValueError(f"props de ventana no permitidas: {sorted(bad)}")

    # Cada prop va en su try/catch: una que esta versión de KWin no tenga no debe tumbar la llamada entera.
    sets = "".join(f"try{{W.{k} = {json.dumps(v)};}}catch(e){{failed.push({json.dumps(k)});}}\n"
                   for k, v in props.items())
    geo = ""
    if geometry is not None:
        x, y, w, h = (float(v) for v in geometry)
        geo = f"W.frameGeometry = {{x:{x}, y:{y}, width:{w}, height:{h}}};\n"

    reply = run_js(_FIND % json.dumps(window_uuid) + f"""
var failed = [];
if (!W) {{ REPLY({{missing: true}}); }} else {{
    var g = W.frameGeometry, before = [g.x, g.y, g.width, g.height];
    {sets}{geo}
    REPLY({{before: before, failed: failed}});
}}""", timeout=timeout)

    if reply.get("missing"):
        raise KWinError(f"KWin ya no tiene ninguna ventana con uuid {window_uuid}.")
    # Fresh read-back instead of a timer inside the script: the script's own QTimer read stale
    # geometry on the clamp edge and reported a rejection that never happened.
    info = window_info(window_uuid)
    if info is None:
        raise KWinError(f"KWin ya no tiene ninguna ventana con uuid {window_uuid}.")
    if geometry is not None:
        # Y la relectura espera un ciclo, porque escribir la posición es síncrono pero cambiar el
        # TAMAÑO no: medido 10 veces, un cambio real de tamaño se traga la primera escritura 3 de 6
        # veces y no llega ni en 5 s, y una segunda escritura lo asienta en ~75 ms (10 de 10, ~317 ms
        # de punta a punta). Leer sin esperar devolvía la geometría vieja sin decir que había fallado,
        # y de ahí salían un test rojo y un viewport del agente a la deriva.
        want = [float(v) for v in geometry]
        deadline = time.monotonic() + GEO_SETTLE
        while [info["x"], info["y"], info["width"], info["height"]] != want:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.08)
            info = window_info(window_uuid) or info
            if [info["x"], info["y"], info["width"], info["height"]] == want:
                break
            run_js(_FIND % json.dumps(window_uuid)
                   + f"if (!W) {{ REPLY({{missing: true}}); }} else {{ {geo} REPLY({{retry: 1}}); }}",
                   timeout=timeout)
            info = window_info(window_uuid) or info
    reply["geometry"] = [info["x"], info["y"], info["width"], info["height"]]
    reply["minimized"] = info["minimized"]
    return reply


# --- el escritorio: estado, ventana activa y activación ---------------------------------------------

# Solo ventanas normales y diálogos (un `kdialog` es diálogo): paneles, escritorio, notificaciones y OSD
# son del shell, no se manejan y quedan fuera. En orden de apilado, de abajo arriba. Sin títulos ni
# resourceClass: esos llegan después por `getWindowInfo`, nunca por el diario (G5).
_STATE = """
var R = function (g) { return [g.x, g.y, g.width, g.height]; };
var id = function (w) { return w ? String(w.internalId).replace(/[{}]/g, "") : null; };
var outs = [], wins = [], S = workspace.screens, L = workspace.stackingOrder;
for (var i = 0; i < S.length; i++) {
    var g = S[i].geometry;
    outs.push({name: String(S[i].name), x: g.x, y: g.y, width: g.width, height: g.height,
               scale: S[i].devicePixelRatio});
}
for (var i = 0; i < L.length; i++) {
    var w = L[i];
    if (w.deleted || !(w.normalWindow || w.dialog)) continue;
    wins.push({uuid: id(w), pid: w.pid, frame: R(w.frameGeometry), client: R(w.clientGeometry),
               output: w.output ? String(w.output.name) : null, minimized: w.minimized,
               active: w === workspace.activeWindow, exclude_from_capture: !!w.excludeFromCapture});
}
var v = workspace.virtualScreenSize, c = workspace.cursorPos;
REPLY({outputs: outs, virtual_screen: [v.width, v.height],
       work_area: R(workspace.clientArea(KWin.WorkArea, workspace.activeScreen, workspace.currentDesktop)),
       cursor: [c.x, c.y], active: id(workspace.activeWindow), windows: wins});
"""


def state() -> dict:
    """Salidas, pantalla virtual, área de trabajo, cursor, ventana activa y ventanas en orden de apilado.

    Un script para todo lo geométrico y un `getWindowInfo` por ventana para título y resourceClass.
    """
    st = run_js(_STATE)
    for w in st["windows"]:
        info = window_info(w["uuid"]) or {}
        w["caption"], w["resource_class"] = info.get("caption"), info.get("resourceClass")
    return st


def active() -> str | None:
    """El uuid de la ventana activa. Corre antes de cada bloque de pulsaciones."""
    return run_js('var w = workspace.activeWindow;'
                  'REPLY({a: w ? String(w.internalId).replace(/[{}]/g, "") : null});')["a"]


def activate(window_uuid: str) -> None:
    """Activa la ventana y comprueba en el mismo script que KWin la dejó activa."""
    if not _UUID.match(window_uuid):
        raise ValueError(f"uuid de ventana con forma inesperada: {window_uuid!r}")
    # de kwin-mcp geometry.py:137-160 (MIT)
    reply = run_js(_FIND % json.dumps(window_uuid) + """
if (W) workspace.activeWindow = W;
REPLY({found: !!W, active: !!W && workspace.activeWindow === W});""")
    if not reply["found"]:
        raise KWinError(f"KWin ya no tiene ninguna ventana con uuid {window_uuid}.")
    if not reply["active"]:
        raise KWinError(f"KWin no dejó activa la ventana {window_uuid}.")


# Traer el navegador delante de Diego (traspaso, PLAN §6): `bringToFront` de CDP no activa un toplevel de Wayland.
# TODAS las ventanas de la clase (la principal y los emergentes, como un «Iniciar sesión con Google»), al centro del
# área útil de la salida principal (`screenOrder[0]`), con su tamaño si cabe, desminimizadas, visibles en la barra y
# el alternador (los repone `park()`) y subidas en su orden de apilado. Con `activate` (Meta+Shift+B: lo pide Diego)
# la de arriba queda activa; sin él (browser_handoff: lo pide el agente) solo pide atención y no roba el foco.
# `excludeFromCapture` se queda: nada la captura.
_AREA = """
var o = (workspace.screenOrder || workspace.screens)[0];
var a = workspace.clientArea(KWin.MaximizeArea, o, workspace.currentDesktop);
"""
_SUMMON = _AREA + """
var L = workspace.stackingOrder, M = [];
for (var i = 0; i < L.length; i++)  // primero se eligen: raiseWindow reordena stackingOrder mientras se recorre
    if (!L[i].deleted && L[i].resourceClass == %s && (L[i].normalWindow || L[i].dialog)) M.push(L[i]);
var top = null, out = [];
for (var i = 0; i < M.length; i++) {
    var W = M[i];
    var g = W.frameGeometry, w = Math.min(g.width, a.width), h = Math.min(g.height, a.height);
    W.minimized = false; W.skipTaskbar = false; W.skipSwitcher = false; W.skipPager = false;
    W.frameGeometry = {x: Math.round(a.x + (a.width - w) / 2), y: Math.round(a.y + (a.height - h) / 2),
                       width: w, height: h};
    try { workspace.raiseWindow(W); } catch (e) {}
    var f = W.frameGeometry;
    out.push({geometry: [f.x, f.y, f.width, f.height], minimized: W.minimized});
    top = W;
}
if (top) { if (%s) workspace.activeWindow = top; else try { top.demandsAttention = true; } catch (e) {} }
REPLY({windows: out, area: [a.x, a.y, a.width, a.height], active: !!top && workspace.activeWindow === top,
       attention: !!top && !!top.demandsAttention});
"""


def work_area() -> list[float]:
    """[x, y, ancho, alto] del área útil (sin paneles) de la salida principal: donde `summon` trae el navegador y
    donde lo deja el modo mirar (`BrowserSession.park` con `visible`)."""
    return run_js(_AREA + "REPLY({a: [a.x, a.y, a.width, a.height]});")["a"]


def summon(resource_class: str, activate: bool = False) -> dict:
    """Pone en pantalla todas las ventanas de `resource_class` (ver `_SUMMON`) y devuelve lo que KWin dejó.
    KWinError si no hay ninguna, si alguna quedó minimizada o fuera del área, o si `activate` y no quedó activa:
    el traspaso solo vale si Diego la tiene delante. Las devuelve `BrowserSession.park()`."""
    if not _LEAF.fullmatch(resource_class):
        raise ValueError(f"resourceClass con forma inesperada: {resource_class!r}")
    r = run_js(_SUMMON % (json.dumps(resource_class), "true" if activate else "false"))
    if not r["windows"]:
        raise KWinError(f"No hay ninguna ventana {resource_class}.")
    ax, ay, aw, ah = r["area"]
    off = [w for w in r["windows"] if w["minimized"] or not (ax <= w["geometry"][0] < ax + aw and ay <= w["geometry"][1] < ay + ah)]
    if off or (activate and not r["active"]):
        raise KWinError(f"KWin no dejó el navegador delante (activa {r['active']}, {r['windows']} sobre {r['area']}).")
    return r


def summon_browser() -> int:
    """`kwin.py --summon-browser`, lo que corre `panic/manos-summon` (Meta+Shift+B): fuera el domador (que ocultaría
    los emergentes que abra Diego; lo vuelve a cargar browser_resume) y todas las ventanas delante, la de arriba
    activa. Sale con 2 si no hay navegador."""
    import sys
    cls = "ManosBrowser"  # browser/session.py RESOURCE_CLASS; sin importarlo: esto corre con el python del sistema
    if not windows(cls):
        print("No hay ningún navegador de manos abierto.", file=sys.stderr)
        return 2
    unload("manostamer")
    print(json.dumps(summon(cls, activate=True)))
    return 0


def shell_pids() -> set[int]:
    """Pids de kwin_wayland y plasmashell en el bus apuntado. `status` pregunta al demonio del bus.

    En vivo, vacío es un fallo (falla cerrado): sin estos pids no se puede negar el shell."""
    out = _busctl("status", "org.kde.KWin", check=False) + "\n" + \
        _busctl("status", "org.kde.plasmashell", check=False)
    pids = {int(line[4:]) for line in out.splitlines() if line.startswith("PID=")}
    if not pids and BUS == "--user":
        raise KWinError("No se encontró el pid de kwin_wayland ni de plasmashell: manos no actúa sin poder negar el shell.")
    return pids


# --- EIS: el canal de teclado y ratón, con guardián -----------------------------------------------

# KWin 6.8.2 se cae (segfault en eis_device_keyboard_send_xkb_modifiers) si se destruye un contexto EIS
# cuyo dispositivo tiene una tecla abajo y el borrado deja un hueco en `m_contexts`: siempre por
# `serviceUnregistered` (el dueño D-Bus muere) y por `disconnect(cookie)` si hay otro contexto detrás.
# Por eso el D-Bus vive en un guardián aparte (`--eis-guard`, sesión propia, ignora TERM/INT/HUP): quien
# pulsa solo tiene el socket EIS; si muere, el guardián sigue vivo y hace `disconnect(cookie)`, que con
# nuestro contexto el último de la lista es seguro (medido: kill -9 y SIGTERM con Shift abajo, 0 caídas).
# techo: se cae igual si (1) matan al guardián con SIGKILL mientras quien pulsa tiene teclas abajo, (2) un
# SIGKILL a todo el cgroup mata a los dos a la vez, o (3) otro cliente RemoteDesktop EIS se conectó
# después del nuestro y sigue vivo cuando el nuestro muere con teclas abajo. Los tres, medidos en anidada.
# Dos sesiones de manos no llegan a (3): `eis.session` toma un cerrojo que el guardián retiene hasta salir.
EIS_CAPS = 1 | 2 | 4 | 32  # libei: POINTER | POINTER_ABSOLUTE | KEYBOARD | BUTTON
EIS_HANGUP_MARGIN = 0.1  # s tras el EOF; colchón: KWin suelta las teclas en disconnect, no al cerrarse el socket, y 0 s dio 10/10


def eis_open(keep_fds=(), timeout: float = 10.0):
    """Arranca el guardián y devuelve (fd EIS, enlace, proceso). Quien llama no toca D-Bus.

    `keep_fds` pasan al guardián y viven lo que él (el cerrojo de la sesión: hasta que haya desconectado).
    """
    import socket
    import sys
    link, end = socket.socketpair()
    try:
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--eis-guard", str(end.fileno()), bus_address()],
                                pass_fds=[end.fileno(), *keep_fds], start_new_session=True, stdin=subprocess.DEVNULL)
    except BaseException:
        link.close()
        raise
    finally:
        end.close()
    link.settimeout(timeout)
    try:
        msg, fds, _, _ = socket.recv_fds(link, 512, 1, socket.MSG_CMSG_CLOEXEC)
    except BaseException:
        link.close()  # el guardián ve EOF, desconecta lo que tenga y sale
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            pass
        raise
    link.settimeout(None)
    if not fds:
        link.close()
        proc.wait(timeout)
        raise KWinError(f"connectToEIS falló: {msg.decode(errors='replace') or 'el guardián murió'}")
    return fds[0], link, proc


def eis_close(link, proc, clean: bool, timeout: float = 5.0) -> bool:
    """Suelta el guardián. `clean`: ninguna tecla abajo y el socket EIS ya cerrado, sin margen. False si no salió."""
    if clean:
        try:
            link.sendall(b".")
        except OSError:
            pass  # guardián ya muerto: que no tape el error original ni se salte la limpieza
    link.close()
    try:
        proc.wait(timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _eis_guard(fd: int, addr: str) -> None:
    import signal
    import socket
    import dbus.bus
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)  # solo muere por EOF del enlace (o SIGKILL: el techo)
    link = socket.socket(fileno=fd)
    conn = dbus.bus.BusConnection(addr)
    try:
        eis, cookie = conn.call_blocking("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop",
                                         "org.kde.KWin.EIS.RemoteDesktop", "connectToEIS", "i", [EIS_CAPS])
    except Exception as e:
        link.sendall(str(e).encode()[:500])
        return
    eis = eis.take()
    socket.send_fds(link, [str(int(cookie)).encode()], [eis])
    os.close(eis)  # nuestra copia fuera: si este proceso muere, el socket EIS no se cierra con él
    clean = False
    try:
        while data := link.recv(64):
            clean = data.endswith(b".")
    except OSError:
        pass  # ECONNRESET: el dueño murió; `clean` sigue False y se aplica el margen
    if not clean:  # el dueño murió o tenía teclas
        time.sleep(EIS_HANGUP_MARGIN)
    try:
        conn.call_blocking("org.kde.KWin", "/org/kde/KWin/EIS/RemoteDesktop",
                           "org.kde.KWin.EIS.RemoteDesktop", "disconnect", "i", [int(cookie)])
    except Exception:
        pass  # KWin o el bus ya no están: no queda nada que desconectar
    finally:
        conn.close()


# --- el script permanente de domado ---------------------------------------------------------------

# Sin él, la ventana del navegador roba el foco 90 s al arrancar (medido: se lo quitó a un juego a
# pantalla completa); con él, 357 microsegundos. Minimiza en `windowAdded` porque una escritura de
# `frameGeometry` NO pega desde dentro del handler: el aparcado fuera de pantalla lo hace el driver
# después, cuando la ventana ya ha completado su configure.
#
# Y el foco, siempre que esté cargado (= aparcado: el traspaso lo descarga). Medido en anidada: al cerrarse la
# ventana activa, KWin activa la siguiente de su cadena de foco, y una ventana aparcada (sin minimizar, fuera de
# pantalla) cuenta como visible: 3 de 3 casos acabaron con la del navegador activa, con otra ventana visible
# incluso. Lo que Diego tecleara iría a la página del agente. Así que cada activación de una ventana de la clase se
# deshace: vuelve la última activa que no es del navegador; si ya no existe, la de más arriba que se pueda; si no
# hay ninguna, el escritorio; y si tampoco, ninguna.
TAMER = """
var CLS = %s, prev = null;
var usable = function (X) {
    return X && !X.deleted && X.resourceClass != CLS && !X.minimized && (X.normalWindow || X.dialog);
};
workspace.windowAdded.connect(function (W) {
    if (W.resourceClass != CLS) return;
    W.skipTaskbar = true; W.skipSwitcher = true; W.skipPager = true;
    try { W.excludeFromCapture = true; } catch (e) {}
    W.minimized = true;
});
workspace.windowActivated.connect(function (W) {
    if (!W) return;
    if (W.resourceClass != CLS) { prev = W; return; }
    var to = usable(prev) ? prev : null, L = workspace.stackingOrder;
    for (var i = L.length - 1; !to && i >= 0; i--) if (usable(L[i])) to = L[i];
    for (var i = 0; !to && i < L.length; i++) if (!L[i].deleted && L[i].desktopWindow) to = L[i];
    workspace.activeWindow = to;
});
"""


def load_tamer(resource_class: str, name: str = "manostamer") -> str:
    """Carga el domador y devuelve la ruta del fichero, que hay que borrar al descargarlo.

    Este script se queda cargado hasta el apagado: es el único que no es efímero.
    """
    fd, path = tempfile.mkstemp(suffix=".js", prefix=name)
    with os.fdopen(fd, "w") as f:
        f.write(TAMER % json.dumps(resource_class))
    unload(name)  # por si una ejecución anterior murió sin limpiar
    load(path, name)
    return path


# --- captura: ScreenShot2 directo ------------------------------------------------------------------

def capture_workspace(fd: int, timeout: float = 5.0) -> dict:
    """Pide a KWin el espacio entero en lógicas por el pipe `fd` y devuelve sus metadatos (width, height,
    stride, format…). KWin contesta y DESPUÉS escribe el pipe desde otro hilo: quien llama cierra su `fd` y
    lee hasta EOF. Un fallo sale como `dbus.DBusException` (la reserva de spectacle se engancha a eso)."""
    import dbus.bus
    import dbus.types
    conn = dbus.bus.BusConnection(bus_address())
    w = dbus.types.UnixFd(fd)  # se queda un dup; take() lo saca para cerrarlo aquí y que llegue el EOF
    try:
        meta = conn.call_blocking("org.kde.KWin", "/org/kde/KWin/ScreenShot2", "org.kde.KWin.ScreenShot2",
                                  "CaptureWorkspace", "a{sv}h",
                                  [{"include-cursor": dbus.Boolean(False), "native-resolution": dbus.Boolean(False)},
                                   w], timeout=timeout)
    finally:
        os.close(w.take())
        conn.close()
    return {str(k): str(v) if isinstance(v, str) else float(v) if isinstance(v, float) else int(v)
            for k, v in meta.items()}


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--eis-guard"]:
        _eis_guard(int(sys.argv[2]), sys.argv[3])
    elif sys.argv[1:2] == ["--summon-browser"]:
        sys.exit(summon_browser())
