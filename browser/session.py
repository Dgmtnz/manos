"""El navegador del agente: ciclo de vida, colocación, diálogos, píxeles y frenos.

Es una ventana headful en la sesión viva del usuario, no un compositor anidado ni un contenedor. Todo
lo que compraba el aislamiento —GPU por hardware, UA sin token `Headless`, `window.screen` real— viene
de ser headful, y el anidamiento costaba ~180 MB, un bus privado y, sobre todo, **el traspaso de
control**: un cliente Wayland no migra entre compositores sin reiniciar.

Las cinco decisiones que este módulo implementa y no vuelve a discutir, todas medidas en esta máquina
el 2026-10-30:

1. **Tubería, nunca un puerto TCP.** Un puerto de depuración en loopback no tiene autenticación, y CDP
   completo sobre este perfil es `Network.getAllCookies` de las sesiones iniciadas del usuario.
   `launch_persistent_context` ya usa la tubería; aquí solo se comprueba, al arrancar, que el árbol no
   abrió **ningún** socket a la escucha.
2. **`Emulation.setFocusEmulationEnabled` en cada sesión CDP nueva, y se queda encendido.** No es una
   mejora: sin él, `Input.*` se **descarta en silencio** —respuesta CDP correcta, nada ocurre en la
   página— siempre que la ventana esté mapeada y sin foco, que es su estado normal. Playwright no lo
   llama. Además alterna `document.visibilityState`, razón de más para no tocarlo a media tarea.
3. **El reposo es fuera de pantalla por posición (x=3400), no minimizado.** Minimizada es el único
   estado en que `CaptureWindow` de KWin devuelve el último frame renderizado sin dar ningún error;
   fuera de pantalla es igual de invisible y sigue renderizando en vivo.
4. **Los píxeles van en su propia sesión CDP, con timeout de 3 s y un reintento en sesión nueva.**
   `Page.captureScreenshot` se cuelga >10 s en el 40-50 % de las llamadas en headful, y las respuestas
   CDP están serializadas por sesión: una captura atascada bloquea todo lo demás de esa sesión. Los
   éxitos están todos por debajo de 227 ms, así que un timeout corto no pierde nada.
5. **Un manejador de diálogos permanente.** `Page.javascriptDialogOpening` informa
   `hasBrowserHandler: true`: en cuanto alguien se suscribe a `Page` —y Playwright lo hace— Chrome deja
   de descartar solo, y somos dueños de cada `alert`, `confirm` y `beforeunload`. Un `prompt()` dentro
   de un `Runtime.evaluate` **no retorna nunca** hasta `handleJavaScriptDialog`, y un `alert` abierto
   colgó una captura 3342 ms. Por eso se expone como `DialogOpen`, un error tipado, y no como un timeout.

Autocomprobación:  uv run --project . tests/test_session.py
"""

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import re
import subprocess
import uuid as _uuid
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import kwin

CHROME = pathlib.Path.home() / ".cache/ms-playwright/chromium-1243/chrome-linux64/chrome"
HOME = pathlib.Path.home() / ".local/share/manos"
# MANOS_BROWSER_PROFILE: el banco de pruebas usa un perfil desechable, sin las sesiones del perfil de siempre
PROFILE = pathlib.Path(os.environ.get("MANOS_BROWSER_PROFILE") or HOME / "browser-profile")
KILL_SWITCH = HOME / "stop"
RESOURCE_CLASS = "ManosBrowser"  # en Wayland nativo --class fija resourceClass; el Brave del usuario
                                 # es "brave-browser", así que la regla no puede alcanzarlo nunca
PARKED_X = 3400.0  # a la derecha del espacio lógico de 3000 de ancho: invisible y sigue renderizando
PARKED_SIZE = (1280.0, 900.0)  # el viewport del agente lo fija park(), no lo que el perfil recuerde
SHOT_TIMEOUT = 3.0  # los éxitos medidos están todos por debajo de 227 ms

FLAGS = [
    "--ozone-platform=wayland",
    f"--class={RESOURCE_CLASS}",
    "--force-renderer-accessibility",
    # Con el parseador de formularios encendido (`autofill.profile_enabled`, abajo), Chrome abre su
    # canal de crowdsourcing: consulta y VOTA a los servidores de Google sobre la FORMA de cada
    # formulario que el agente visita —las firmas, no los valores—. Esto lo cierra, y está medido que
    # no cuesta nada: el relleno sigue en 6 de 6 (research/p2/E-autofill.md §9.2).
    "--disable-features=AutofillServerCommunication",
    # WebMCP: las tools que declara la página (browser/webmcp.py). Experimental y tras flag; nada depende de ella.
    # Sin --enable-webmcp-testing: medido que getTools/executeTool y frame.webmcp de Playwright van sin él.
    "--enable-features=WebMCP",
    "--password-store=basic",
    "--mute-audio",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]
# NO van aquí, y cada ausencia es una medida: --window-position y --window-size son INERTES en Wayland
# nativo (se pidió 1920,420 1080x1080 y KWin puso 436,622 1048x1038 en la otra salida), y
# --remote-debugging-port abriría CDP sin autenticación sobre este perfil.
# Tampoco va --no-sandbox, y eso NO es gratis: Playwright lo inyecta solo si `chromium_sandbox` es
# falso, que es su defecto. Medido leyendo `Seccomp:` de /proc/<renderer>/status: con el defecto los
# tres renderers salen con `Seccomp: 0` —sin filtro— y con `chromium_sandbox=True` salen con
# `Seccomp: 2`. El agente navega páginas arbitrarias en la sesión viva del usuario, así que el sandbox
# va encendido por argumento explícito y --diag lo comprueba en cada arranque.

# El gestor de contraseñas y el autorrelleno de la UI de Chromium, apagados antes del primer arranque:
# un diálogo de "¿guardar contraseña?" tapa la página y no es contenido al que CDP llegue.
# `autofill.enabled` no está: está deprecado y Chrome 153 lo tira al reescribir Preferences.
PREFS = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile.password_manager_enabled": False,
    "profile.password_manager_leak_detection": False,
    # El nombre engaña y por eso lleva comentario: esto NO enciende las sugerencias de direcciones
    # guardadas —no hay ninguna guardada— sino el PARSEADOR de formularios, que es lo que
    # `Autofill.trigger` necesita. Con los tres (`autofill.profile_enabled`,
    # `autofill.credit_card_enabled`, `credentials_enable_service`) en False, Chrome no parsea y
    # `trigger` contesta "Field not found" en todos los campos de todos los formularios: bisecado, 2
    # ejecuciones por combinación, cualquiera de los tres a True lo arregla (research/p2/E-autofill.md
    # §2). Este es el más barato de los tres. Es un estrechamiento deliberado de la postura de
    # credenciales de PLAN §7.2 y esto es lo que cuesta, medido leyendo el sqlite del perfil
    # (`Default/Web Data`) con el pref a False y a True, un envío real por lado:
    #
    #   · `addresses` y `credit_cards`: 0 filas en los dos casos, y ningún aviso de «guardar
    #     dirección». El Autofill de CDP no guarda nada y el de la UI no se usa nunca.
    #   · `autofill` (el historial de autocompletado por campo): con el pref a False, enviar un
    #     formulario NO añade ninguna fila; con el pref a True añade UNA POR CAMPO de texto no vacío
    #     —medido: `correo=prefon@example.es`, `motivo=motivo-prefon`—. Las contraseñas no entran (un
    #     `type=password` nunca va a esa tabla) y las tarjetas tampoco (`credit_card_enabled` sigue en
    #     False), pero **un secreto de nivel B que el agente sí teclea —un OTP en un `type=text`— acaba
    #     en disco al enviar**. Es local, no va a sync ni a backup, y se revoca borrando el directorio
    #     del perfil, que es la única garantía que §7.2 da. Queda dicho porque es una ruta de datos en
    #     reposo nueva, y si el precio no se acepta la decisión es de §7.2, no de aquí.
    "autofill.profile_enabled": True,
    "autofill.credit_card_enabled": False,
}

WRAPPER = """#!/bin/sh
# Mete el navegador en su propio scope de systemd para tener cgroup.freeze como botón de pánico
# (medido: congelar 9 ms, descongelar 10 ms, reversible con el websocket CDP vivo). `exec` conserva
# los fd 3 y 4, que son la tubería CDP; sin `exec` el transporte se pierde.
# Chrome se queda solo con el ÚLTIMO --disable-features: el nuestro tiraba la lista entera de
# Playwright (medido: ThirdPartyStoragePartitioning volvía a encenderse). Aquí se funden en uno.
# Igual --enable-features: el WebMCP nuestro tiraba el CDPScreenshotNewSurface de Playwright.
off= on= n=$#
for a in "$@"; do
    case $a in
        --disable-features=*) off="$off,${a#--disable-features=}" ;;
        --enable-features=*) on="$on,${a#--enable-features=}" ;;
        *) set -- "$@" "$a" ;;
    esac
done
shift "$n"
[ -n "$off" ] && set -- "--disable-features=${off#,}" "$@"
[ -n "$on" ] && set -- "--enable-features=${on#,}" "$@"
exec systemd-run --user --scope --quiet --collect --unit="$MANOS_SCOPE" -- "$MANOS_CHROME" "$@"
"""


def forbidden(path) -> bool:
    """La regla de rutas de manos, una sola (subidas, `file://`, aserciones de ficheros): vedado todo lo que tenga
    un componente con punto bajo la casa (ahí viven las credenciales: ~/.ssh, ~/.gnupg, ~/.config/gh,
    ~/.local/share/kwalletd, ~/.claude…; una lista se queda siempre corta), /proc, /sys, /dev y el perfil del
    navegador. Se mira la ruta tal cual y adonde lleva: un enlace no saca nada de ahí."""
    homes = {pathlib.Path.home(), pathlib.Path(os.path.realpath(pathlib.Path.home()))}
    banned = [pathlib.Path(v) for v in ("/proc", "/sys", "/dev")] + [PROFILE, pathlib.Path(os.path.realpath(PROFILE))]
    p = os.path.expanduser(str(path))
    for cand in (pathlib.Path(os.path.normpath(os.path.abspath(p))), pathlib.Path(os.path.realpath(p))):
        if any(cand.is_relative_to(h) and any(x.startswith(".") for x in cand.relative_to(h).parts) for h in homes):
            return True
        if any(cand == b or cand.is_relative_to(b) for b in banned):
            return True
    return False


class DialogOpen(RuntimeError):
    """Hay un diálogo nativo abierto y nada más puede avanzar en esa página; el mensaje es para el modelo."""


class Cancelled(RuntimeError):
    """La petición se canceló o alguien tocó la parada de emergencia; el mensaje es para el modelo."""


class Busy(RuntimeError):
    """Ya hay una llamada tocando este navegador; el mensaje es para el modelo."""


class UserDriving(RuntimeError):
    """Diego conduce el navegador (traspaso): manos no lo toca ni lo mira hasta `browser_resume`."""


def driving_flag() -> pathlib.Path:
    """`$XDG_DATA_HOME/manos/driving`, leído en cada llamada (la anidada y el banco cambian XDG_DATA_HOME).

    Lo escriben `browser_handoff` y `panic/manos-summon` (Meta+Shift+B); solo lo borra `browser_resume`. El
    fichero ES el estado del traspaso: sin él, el agente conduce.

    Sin época de vallado (PLAN §6), y es una decisión: la época servía para que una llamada en vuelo notara
    un traspaso y una vuelta (A→B→A) ocurridos mientras corría. Aquí esa vuelta no puede intercalarse:
    `browser_resume` toma `exclusive()` antes de aparcar, así que espera a la llamada en vuelo, y cada tool
    mira el fichero en `check()` antes de empezar y otra vez al coger el candado. Lo que queda (Meta+Shift+B
    a media acción) lo para el siguiente `check()`, igual que lo pararía una época: ninguna de las dos corta
    una llamada CDP ya enviada.
    """
    return pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local/share") / "manos" / "driving"


LOCK_WAIT = 10.0  # s que una llamada espera el candado antes de dar Busy (Haiku lanza dos a la vez y no se rehace)


@contextlib.asynccontextmanager
async def hold(lock: asyncio.Lock, busy: str):
    """Coge `lock` esperando hasta LOCK_WAIT s; si no llega, `Busy(busy)`. Cancelar la espera (el SDK cancela el
    scope al recibir `notifications/cancelled`) sale en el acto sin tocar nada de quien tiene el candado."""
    try:
        async with asyncio.timeout(LOCK_WAIT):
            await lock.acquire()
    except TimeoutError:
        raise Busy(busy) from None
    try:
        yield
    finally:
        lock.release()


def _seed_prefs() -> None:
    """Mezcla PREFS en el Preferences del perfil. Idempotente: se puede correr antes de cada arranque."""
    path = PROFILE / "Default" / "Preferences"
    path.parent.mkdir(parents=True, exist_ok=True)
    prefs = json.loads(path.read_text()) if path.exists() else {}
    for dotted, value in PREFS.items():
        node = prefs
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    path.write_text(json.dumps(prefs))


def _cgroup(scope: str) -> pathlib.Path:
    return pathlib.Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice"
                        f"/user@{os.getuid()}.service/app.slice/{scope}.scope")


def renderer_seccomp(cgroup: pathlib.Path) -> list[str]:
    """El modo seccomp de cada proceso renderer del árbol. `2` es filtro puesto; `0` es sin sandbox.

    Es la comprobación que convierte "el sandbox está encendido" en un hecho leído del kernel, igual
    que `_assert_no_listening_sockets` hace con la tubería. Playwright apaga el sandbox por defecto, y
    un `--no-sandbox` silencioso no se nota de ninguna otra forma.
    """
    modes = []
    for pid in cgroup.joinpath("cgroup.procs").read_text().split():
        try:
            if "--type=renderer" not in pathlib.Path(f"/proc/{pid}/cmdline").read_text():
                continue
            status = pathlib.Path(f"/proc/{pid}/status").read_text()
        except OSError:  # el proceso pudo morir entre la lista y la lectura
            continue
        modes += [l.split()[1] for l in status.splitlines() if l.startswith("Seccomp:")]
    return modes


def _browser_main(cgroup: pathlib.Path, proc=pathlib.Path("/proc"),
                  chrome=CHROME) -> tuple[str, bool] | None:
    """El proceso principal de Chrome y si se ha salido del scope; None si no se le ve.

    Dentro: un proceso del scope que ejecuta `chrome` sin `--type=`. Fuera: el padre, fuera del scope,
    de un proceso del scope, y que ejecuta `chrome`. Nunca por el nombre de su unidad: los scopes de
    Brave se llaman igual.
    """
    procs = set(cgroup.joinpath("cgroup.procs").read_text().split())
    chrome = chrome.resolve()
    for pid in procs:
        try:
            ppid = proc.joinpath(pid, "stat").read_text().rsplit(")", 1)[1].split()[1]
            if ppid not in procs and proc.joinpath(ppid, "exe").resolve() == chrome:
                return ppid, True
            if (proc.joinpath(pid, "exe").resolve() == chrome
                    and "--type=" not in proc.joinpath(pid, "cmdline").read_text()):
                return pid, False
        except OSError:  # murió entre la lista y la lectura, o es un proceso ajeno
            continue
    return None


def _assert_no_listening_sockets(cgroup: pathlib.Path) -> None:
    """El árbol del navegador no puede tener ni un socket a la escucha. Es la aserción que convierte
    'usamos la tubería' en un hecho comprobado en cada arranque, no en una intención."""
    pids = set(cgroup.joinpath("cgroup.procs").read_text().split())
    out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, check=True).stdout
    listening = {m for line in out.splitlines() for m in re.findall(r"pid=(\d+)", line)}
    leaked = pids & listening
    if leaked:
        raise RuntimeError(f"El navegador abrió un socket a la escucha (pids {sorted(leaked)}). "
                           "CDP por TCP no tiene autenticación y este perfil tiene sesiones iniciadas.")


class BrowserSession:
    """El navegador del agente. Se crea con `await BrowserSession.start()` y se cierra con `close()`."""

    def __init__(self, playwright, context, scope: str, tamer_path: str):
        self._pw = playwright
        self.context = context
        self.scope = scope
        self.cgroup = _cgroup(scope)
        self._tamer_path: str | None = tamer_path  # None = domador descargado (y su fichero borrado)
        self._cdp: dict[int, object] = {}   # id(page) -> sesión CDP con emulación de foco encendida
        self._pixels = None                 # sesión CDP aparte, solo para capturas
        self._pixels_page = None
        self.dialog = None                  # el diálogo abierto, si lo hay
        self.window_uuid: str | None = None
        self.main_pid: str | None = None    # el proceso principal, devuelto al scope en start()
        self._closing = False
        self._lock = asyncio.Lock()
        self._watch = None                  # la tarea de `_watch_driving`
        self._handed = False                # traspaso en curso: sin domador ni selector de ficheros interceptado
        self.choosers = 0                   # selectores de ficheros nativos parados (los que no pidió act)
        self.visible = False                # modo mirar (set_visible): a la vista y sin domador, control compartido

    # --- arranque ---------------------------------------------------------------------------------

    @classmethod
    async def start(cls, url: str = "about:blank", timeout: float = 30.0) -> "BrowserSession":
        from playwright.async_api import async_playwright

        if not CHROME.exists():
            raise FileNotFoundError(
                f"No está el binario de Chrome for Testing en {CHROME}. manos no arranca sin él: "
                "instálalo con 'uv run playwright install chromium' o corrige la ruta.")
        HOME.mkdir(parents=True, exist_ok=True)
        _seed_prefs()

        wrapper = HOME / "chrome-scope.sh"
        wrapper.write_text(WRAPPER)
        wrapper.chmod(0o755)
        scope = f"manos-browser-{os.getpid()}-{_uuid.uuid4().hex[:6]}"

        # El domador se carga ANTES de lanzar: sin él la ventana roba el foco 90 s al mapearse.
        tamer_path = kwin.load_tamer(RESOURCE_CLASS)

        pw = await async_playwright().start()
        try:
            context = await pw.chromium.launch_persistent_context(
                str(PROFILE),
                executable_path=str(wrapper),
                headless=False,
                args=FLAGS,
                env={**os.environ, "MANOS_SCOPE": scope, "MANOS_CHROME": str(CHROME)},
                no_viewport=True,
                chromium_sandbox=True,  # el defecto de Playwright es False, e inyecta --no-sandbox
                timeout=timeout * 1000,
            )
        except BaseException:
            kwin.unload("manostamer")
            os.unlink(tamer_path)
            await pw.stop()
            raise

        self = cls(pw, context, scope, tamer_path)
        try:
            if not self.cgroup.exists():
                raise RuntimeError(f"El scope {scope} no apareció en {self.cgroup}: sin cgroup no hay "
                                   "botón de pánico. ¿Falta systemd-run?")
            # Chrome se muda solo a `app-org.chromium.Chromium-<pid>.scope` (lo hacen igual Brave y
            # Electron) y aquí deja solo a los hijos. Sin devolverlo, ni el pánico ni el `stop` del
            # cierre tocarían al proceso principal, y la aserción de sockets no lo vería. Escribirlo en
            # `cgroup.procs` lo devuelve; systemd recoge el scope vacío solo. Si no se muda nunca (un
            # Chrome futuro, Flatpak), dentro es justo donde lo queremos. Pero «dentro» se acepta solo
            # al final de la espera, y es una medida: la mudanza llega 0,6-1,1 s tras lanzar y el
            # primer sondeo cae a 1,3-1,9 s; aceptarlo antes dejaría que se escapase después.
            found = None
            for _ in range(60):
                found = _browser_main(self.cgroup)
                if found and found[1]:
                    break
                await asyncio.sleep(0.05)
            if found is None:
                raise RuntimeError("No se encuentra el proceso principal de Chrome, ni en su scope ni "
                                   "fuera: sin él no hay botón de pánico fiable.")
            self.main_pid, escaped = found
            if escaped:
                self.cgroup.joinpath("cgroup.procs").write_text(self.main_pid)
            _assert_no_listening_sockets(self.cgroup)
            # Toda carga file:// (también un enlace dentro de una página file://) pasa la regla de rutas.
            await context.route("file://**", self._file_route)
            # Banners de cookies: rechaza o «solo necesarias», nunca acepta (browser/consent.js), en cada frame.
            await context.add_init_script(path=pathlib.Path(__file__).with_name("consent.js"))
            context.on("page", lambda page: asyncio.ensure_future(self._wire(page)))
            for page in context.pages:
                await self._wire(page)
            info = await asyncio.to_thread(kwin.wait_window, RESOURCE_CLASS, timeout)
            self.window_uuid = info["uuid"]
            await self.park()
            self._watch = asyncio.ensure_future(self._watch_driving())
            if url != "about:blank":
                await self.page.goto(url)
        except BaseException:
            await self.close()
            raise
        return self

    @property
    def page(self):
        if not self.context.pages:
            raise RuntimeError("El navegador no tiene ninguna página abierta.")
        return self.context.pages[-1]

    # --- una llamada a la vez ---------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """Serializa las llamadas que tocan la página: espera hasta LOCK_WAIT s y luego lanza `Busy`. Ya
        dentro, mira la parada de emergencia (pudo tocarse durante la espera).

        No es teórico: el SDK atiende `tools/call` concurrentes, y dos observaciones solapadas se
        retiran los refs la una a la otra —la primera devuelve al modelo un listado entero de refs que
        ya no resuelven— y se sueldan entre sí el tapado de secretos de la captura. Fallar en el acto
        tampoco valía: Haiku lanza dos llamadas a la vez y no se recupera del `Busy` (bench, P5).
        """
        async with hold(self._lock, f"El navegador lleva {LOCK_WAIT:g} s atendiendo otra llamada. Espera a "
                                    "que conteste y vuelve a intentarlo."):
            check(self)
            yield
        # ponytail: un candado por navegador, que es exactamente lo que hay en esta fase. El día que
        # haya varias páginas vivas, uno por diana (PLAN §11.2).

    # --- sesiones CDP -----------------------------------------------------------------------------

    async def cdp(self, page=None):
        """La sesión CDP de una página, con la emulación de foco ya encendida. Único camino: una sesión
        creada a mano y sin ella descarta `Input.*` en silencio."""
        page = page or self.page
        session = self._cdp.get(id(page))
        if session is None:
            session = await self.context.new_cdp_session(page)
            await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            # Un diálogo que se cierra fuera de browser_dialog (Diego en su ventana) deja de bloquear.
            session.on("Page.javascriptDialogClosed", lambda _e: setattr(self, "dialog", None))
            await session.send("Page.enable")
            self._cdp[id(page)] = session
        return session

    async def _file_route(self, route) -> None:
        """`file://` solo a un fichero regular fuera de lo vedado (`forbidden`): sin esto, un enlace en una página
        local llevaba a ~/.ssh y el modo texto lo leía."""
        path = url2pathname(unquote(urlsplit(route.request.url).path))
        if forbidden(path) or not os.path.isfile(os.path.realpath(path)):
            await route.abort("accessdenied")
        else:
            await route.continue_()

    def _drop_chooser(self, _chooser) -> None:
        """El selector de ficheros nativo es un portal en el escritorio de Diego: con este oyente Playwright lo
        intercepta y aquí se descarta. Las subidas van por `browser_act upload` (su propio `expect_file_chooser`)."""
        self.choosers += 1

    async def _wire(self, page) -> None:
        page.on("dialog", self._on_dialog)
        if not self._handed:
            page.on("filechooser", self._drop_chooser)
        page.on("close", lambda p: self._cdp.pop(id(p), None))
        await self.cdp(page)

    # --- diálogos ---------------------------------------------------------------------------------

    def _on_dialog(self, dialog) -> None:
        if self._closing:  # un beforeunload al cerrar no puede bloquear el apagado
            asyncio.ensure_future(dialog.accept())
            return
        self.dialog = dialog

    def require_no_dialog(self) -> None:
        """Lo que llama toda tool que no puede avanzar con un diálogo abierto. Es un error tipado a
        propósito: sin él esto llega como un timeout que no dice nada."""
        if self.dialog is not None:
            raise DialogOpen(
                f'Hay un diálogo {self.dialog.type} abierto en la página que dice "{self.dialog.message}" '
                "y bloquea todo lo demás. Respóndelo con browser_dialog(accept=true) o "
                "browser_dialog(accept=false) antes de seguir.")

    async def answer_dialog(self, accept: bool = True, text: str = "") -> None:
        if self.dialog is None:
            raise RuntimeError("No hay ningún diálogo abierto que responder.")
        dialog, self.dialog = self.dialog, None
        await (dialog.accept(text) if accept else dialog.dismiss())

    # --- píxeles ----------------------------------------------------------------------------------

    async def _pixel_session(self):
        if self._pixels is None or self._pixels_page is not self.page:
            self._pixels_page = self.page
            self._pixels = await self.context.new_cdp_session(self._pixels_page)
        return self._pixels

    async def screenshot(self, fmt: str = "jpeg", quality: int = 70) -> bytes:
        """Una captura de la página. Timeout de 3 s y un reintento **en sesión nueva**: una captura
        atascada bloquea su sesión entera, así que reintentar en la misma no serviría de nada."""
        self.require_no_dialog()
        params = {"format": fmt} | ({"quality": quality} if fmt == "jpeg" else {})
        for attempt in (1, 2):
            session = await self._pixel_session()
            try:
                out = await asyncio.wait_for(
                    session.send("Page.captureScreenshot", params), SHOT_TIMEOUT)
                return base64.b64decode(out["data"])
            except asyncio.TimeoutError:
                self._pixels, self._pixels_page = None, None  # la sesión queda envenenada, se tira
                if attempt == 2:
                    raise TimeoutError(
                        "Page.captureScreenshot se colgó dos veces seguidas. Es un defecto conocido de "
                        "headful en esta máquina (40-50 % de las llamadas). Inténtalo de nuevo o usa "
                        "una captura de pantalla del escritorio.")

    # --- colocación -------------------------------------------------------------------------------

    async def park(self, tries: int = 10) -> dict:
        """Deja la ventana en reposo: fuera de pantalla por posición, con `PARKED_SIZE`, sin barra de
        tareas, sin alternador, sin paginador y fuera de las capturas de escritorio. **No** minimizada.

        Escribe la geometría **completa**, y eso corrige una medida anterior que decía que KWin rechaza
        en silencio cualquier resize con la ventana fuera de toda salida. Vuelto a medir: 8 de 8
        resizes fuera de pantalla **pegan**, con relectura exacta. La consecuencia de creerse lo
        contrario era que el viewport del agente lo decidía el `browser.window_placement` que el perfil
        persistente recuerda —se leyó un viewport de 500x151 px, con el que la cosecha ve 4 elementos
        de 11—, así que el tamaño lo fija aquí quien también fija la posición.

        **Se comprueban las dos cosas, y no significan lo mismo.** La posición es la propiedad de
        *seguridad* —si no se cumple, la ventana está a la vista del usuario y manos no sigue—; el
        tamaño es la de *corrección*, porque de él dependen la oclusión de la cosecha y las capturas.
        Antes solo se verificaba la posición y se devolvía éxito, así que el viewport se quedaba con el
        tamaño que KWin tuviera: medido, ocho `park()` seguidos con la ventana clavada en 1180x800, que
        es exactamente la deriva que esta función existe para eliminar. Si tras los reintentos la
        posición está y el tamaño no, se devuelve con `clamped: True` en vez de abortar: KWin aplica el
        tamaño con su propio clamp (una ventana de Chrome no baja de ~500 px de ancho) y perder el
        navegador por 20 px de ancho sería peor que decirlo.

        Se reintenta porque hay dos carreras. Al arrancar, KWin anuncia la ventana en cuanto se mapea
        pero vuelve a colocarla cuando la superficie confirma su tamaño, y esa colocación se come una
        escritura anterior (en frío se vio aterrizar en x=503 con la escritura ya hecha). Y un cambio de
        tamaño se **traga la primera escritura** la mitad de las veces: medido, 3 de 6 cambios reales no
        llegaron en 5 s con una sola escritura y 10 de 10 llegaron en ~317 ms con una segunda.
        `kwin.window_set` ya reintenta por dentro; esto es el cinturón sobre el tirante.

        **Modo mirar** (`self.visible`, ver `set_visible`): el mismo reposo pero a la vista, en la esquina inferior
        derecha del área útil de la salida principal (`kwin.work_area`, la de `summon`), con `PARKED_SIZE` recortado
        a esa área, en la barra de tareas y el alternador para que Diego la encuentre. `park()` no la activa;
        `excludeFromCapture` se queda. La comprobación es entonces la posición pedida, no x ≥ PARKED_X.
        """
        hide, size, at = not self.visible, PARKED_SIZE, None
        if self.visible:
            ax, ay, aw, ah = await asyncio.to_thread(kwin.work_area)
            size = (float(min(PARKED_SIZE[0], aw)), float(min(PARKED_SIZE[1], ah)))
            at = (float(ax + aw - size[0]), float(ay + ah - size[1]))
        parked = None
        for _ in range(tries):
            info = await asyncio.to_thread(kwin.window_info, self.window_uuid)
            if info is None:
                raise RuntimeError("KWin perdió la ventana del navegador mientras se aparcaba.")
            reply = await asyncio.to_thread(
                kwin.window_set, self.window_uuid,
                (*(at or (PARKED_X, info["y"])), *size),
                minimized=False, skipTaskbar=hide, skipSwitcher=hide, skipPager=hide,
                excludeFromCapture=True)
            await asyncio.sleep(0.1)
            info = await asyncio.to_thread(kwin.window_info, self.window_uuid)
            if info is None or (info["x"] < PARKED_X if hide else (info["x"], info["y"]) != at):
                continue
            reply["geometry"] = [info["x"], info["y"], info["width"], info["height"]]
            if (info["width"], info["height"]) == size:
                return reply
            parked = reply
        if parked is not None:
            parked["clamped"] = True
            return parked
        if not hide:
            raise RuntimeError(f"KWin no dejó la ventana del navegador en {at} tras {tries} intentos.")
        raise RuntimeError(f"KWin no dejó la ventana del navegador fuera de pantalla tras {tries} "
                           "intentos: estaría a la vista del usuario, así que manos no sigue.")

    async def set_visible(self, visible: bool) -> None:
        """Modo mirar, **control compartido**: a la vista y SIN domador, así que Diego puede hacer clic y teclear en
        la ventana (un captcha) mientras el agente sigue conduciendo, y los emergentes salen normales. Al esconderla,
        el domador vuelve antes de mover la ventana: fuera de pantalla nunca va sin él. Al mostrarla se descarga
        después de colocarla, así que el arranque (`start`, que lo carga antes de lanzar) sigue cubierto."""
        if not visible and not self._handed:
            await self._tamer(True)
        self.visible = visible
        try:
            await self.park()
        except BaseException:
            self.visible = not visible  # la siguiente llamada vuelve a colocarla
            raise
        finally:
            if not self._handed:  # en un traspaso manda hand_over/take_back
                await self._tamer(not self.visible)

    async def _tamer(self, on: bool) -> None:
        """Domador cargado o fuera; su fichero solo existe mientras está cargado. Idempotente."""
        if on == (self._tamer_path is not None):
            return
        if on:
            self._tamer_path = await asyncio.to_thread(kwin.load_tamer, RESOURCE_CLASS)
            return
        await asyncio.to_thread(kwin.unload, "manostamer")
        path, self._tamer_path = self._tamer_path, None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    # --- traspaso -----------------------------------------------------------------------------------

    async def hand_over(self) -> None:
        """Lo que el traspaso le devuelve a Diego: sin domador (un emergente que abra, como «Iniciar sesión con
        Google», nacería minimizado y oculto) y con el selector de ficheros nativo."""
        self._handed = True
        await self._tamer(False)
        for page in self.context.pages:
            with contextlib.suppress(KeyError):  # pyee: quitar uno que no está lanza KeyError
                page.remove_listener("filechooser", self._drop_chooser)

    async def _watch_driving(self) -> None:
        """Meta+Shift+B no pasa por el servidor: escribe el fichero y trae la ventana desde otro proceso, que no puede
        quitar el oyente `filechooser` de esta sesión. Esto lo nota (cada segundo, mientras viva el navegador) y hace
        el resto de `hand_over`. La vuelta sigue siendo solo `browser_resume`."""
        while True:
            await asyncio.sleep(1.0)
            if not self._handed and driving():
                with contextlib.suppress(Exception):
                    await self.hand_over()

    async def take_back(self) -> None:
        """Deshace `hand_over`: domador otra vez (salvo en modo mirar, que va sin él) y selector interceptado."""
        await self._tamer(not self.visible)
        self._handed = False
        for page in self.context.pages:
            with contextlib.suppress(KeyError):
                page.remove_listener("filechooser", self._drop_chooser)  # nunca dos veces
            page.on("filechooser", self._drop_chooser)

    # --- frenos -----------------------------------------------------------------------------------

    def freeze(self) -> None:
        """Congela el árbol entero. Es el primer peldaño del pánico: acotado, reversible y mucho más
        barato que parar el scope, que sobre un árbol de navegador tiene una cola de 2,4 s. La
        escritura vuelve en menos de 1 ms (medido 0,3-0,6) y el cgroup queda `frozen 1` al instante."""
        self.cgroup.joinpath("cgroup.freeze").write_text("1")

    def thaw(self) -> None:
        self.cgroup.joinpath("cgroup.freeze").write_text("0")

    # --- recuperación de caídas -------------------------------------------------------------------

    async def recover(self):
        """Tras una caída del renderer. Medido y contraintuitivo: `Inspector.targetCrashed` **no se
        dispara nunca** y `Page.reload` también da timeout. Lo que sobrevive es la conexión del
        navegador, así que se descarta el target y se crea otro por ahí. Nunca `pkill`."""
        dead = self.page
        self._cdp.pop(id(dead), None)
        self._pixels, self._pixels_page = None, None
        self.dialog = None
        # El target nuevo va ANTES de cerrar el caído, y ese orden es una medida: cerrar la última
        # página de un contexto persistente se lleva la ventana por delante, y entonces
        # `Target.createTarget` falla con "Failed to open a new tab".
        page = await self.context.new_page()
        try:  # cerrar una página caída puede no volver nunca: acotarlo o la recuperación se cuelga
            await asyncio.wait_for(dead.close(run_before_unload=False), 5.0)
        except Exception:
            pass
        await self._wire(page)
        return page

    # --- apagado ----------------------------------------------------------------------------------

    async def close(self) -> None:
        """Cierre y recogida del scope. Nunca `pkill -f`, que empareja la línea de comandos del propio
        shell que lo invoca y lo mata.

        En el camino feliz la tubería basta: matar al padre cierra los fd 3 y 4 y el árbol entero sale.
        En el camino de fallo **no**: tras un `test_session.py` que murió a mitad quedaron dos
        `chrome_crashpad_handler` vivos, reparentados a pid 1 y con el scope sin recoger. Por eso esto
        termina parando el scope, que es la red que de verdad los barre y la razón de que el scope no
        sea decorativo. `cgroup.freeze` se deshace primero: un árbol congelado por la parada de
        emergencia no contestaría nunca a `context.close()`.
        """
        self._closing = True
        if self._watch is not None:
            self._watch.cancel()
        try:
            self.thaw()
        except OSError:
            pass
        try:
            # Las pestañas antes que el navegador, y es una medida: `Browser.close` (lo que manda
            # `context.close()`) con una pestaña en su PRIMERA navegación real tumba al proceso
            # principal con SIGTRAP —un CHECK de Chrome, un core por sesión—. Cerrando las pestañas
            # Chrome sale solo por el camino normal, con código 0. Acotado: una caída no vuelve nunca.
            for page in self.context.pages:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(page.close(run_before_unload=False), 5.0)
            await self.context.close()
        finally:
            kwin.unload("manostamer")  # un script que se queda cargado sobrevive al proceso
            if self._tamer_path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._tamer_path)
            await self._pw.stop()
            # --collect borra la unidad al pararla; si ya no existe, `stop` no hace nada y no importa.
            await asyncio.to_thread(subprocess.run, ["systemctl", "--user", "stop",
                                                     f"{self.scope}.scope"], capture_output=True)


# --- parada de emergencia ---------------------------------------------------------------------------
# Solo el fichero kill-switch, que **no pasa por el servidor en absoluto** (PLAN §8.3). La cancelación
# del protocolo no se reimplementa aquí: el SDK instalado trae `PeerCancelMode = "interrupt"` por
# defecto y, al recibir `notifications/cancelled`, cancela el scope del handler
# (`mcp/shared/jsonrpc_dispatcher.py:644`). Eso corta en el siguiente `await`, no solo donde alguien se
# acordó de llamar a `check()`, así que el registro de `asyncio.Event` por petición que había aquí era
# una señal peor escrita dos veces.


def check(session: "BrowserSession | None" = None) -> None:
    """El punto de control, delante de cada acción que muta. Lanza `Cancelled`, `UserDriving` o no hace nada.

    Congela el árbol antes de lanzar, que es el primer peldaño de la escalera de pánico del PLAN §8.3
    (tocar el freno → congelar → decidir → parar): 10 ms, acotado y reversible. Sin esto, tocar el
    freno solo impedía *empezar* la siguiente llamada, y lo que ya estuviera en vuelo seguía navegando.
    """
    check_stop(session)
    require_not_driving()


def check_stop(session: "BrowserSession | None" = None) -> None:
    """Solo el freno (`check` sin el traspaso): lo que mira `browser_resume`, que corre con el fichero aún puesto."""
    if KILL_SWITCH.exists():
        if session is not None:
            with contextlib.suppress(OSError):
                session.freeze()
        raise Cancelled("Parada de emergencia activada: alguien tocó el freno. El navegador queda "
                        "congelado. No reintentes; el servidor no volverá a actuar hasta que se borre "
                        f"{KILL_SWITCH}.")


def driving() -> bool:
    """¿Conduce Diego? Falla cerrado: un fichero que no se puede mirar (permisos, E/S) cuenta como que sí.

    Un traspaso de `browser_handoff` lleva el pid del servidor que lo dio; muerto ese servidor, su navegador
    (tubería CDP) murió con él y no queda nada que conducir: el fichero se borra, como hace manos-summon sin
    navegador. El de Meta+Shift+B no lleva pid y sigue hasta browser_resume.
    """
    flag = driving_flag()
    try:
        pid = json.loads(flag.read_text()).get("pid")
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return True
    if isinstance(pid, int) and pid > 0 and not _alive(pid):
        # ponytail: leer y borrar no es atómico; un traspaso nuevo escrito justo entre medias se perdería
        flag.unlink(missing_ok=True)
        return False
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: existe, de otro usuario
        return True
    return True


def require_not_driving() -> None:
    """Mientras Diego conduce: ni actuar, ni capturas ni snapshots (PLAN §6), tampoco del escritorio."""
    if driving():
        why = ""
        with contextlib.suppress(OSError, ValueError, AttributeError):
            why = f" (motivo: {json.loads(driving_flag().read_text()).get('reason', '')})"
        raise UserDriving(f"Diego conduce el navegador{why}; manos no actúa ni mira hasta browser_resume.")
