"""`browser_act`: un verbo sobre un ref de `browser_observe`, con lo que cambió dicho barato.

Las piezas son las de `forms.py` (clic de ratón confiable con hit-test, `Input.insertText`, la selección de un
`<select>` como la hace Playwright, `DOM.setFileInputFiles`); aquí solo se añade la identidad del ref:

- **Un ref es un nodo concreto, no un xpath que casualmente exista.** Se actúa sobre `__MANOS_NODES[i]` del
  MISMO mundo aislado que lo acuñó; si el mundo cambió (la página navegó), si el nodo se desprendió, o si su xpath
  o su nombre accesible ya no son los observados (otra cosecha reordenó el array, la página lo repintó),
  `StaleHandle`. Nunca se recrea el mundo para actuar.
- **Contraseña, tarjeta, CVC, OTP (nivel A de `sanitize.classify`) o un iframe de pasarela: no se teclea, no se
  elige y no se pulsan teclas**, igual que en `form_fill`; es un traspaso. Vale también para el campo que acaba
  con el foco (un elemento que delega el foco en una contraseña). Lo tecleado no vuelve nunca: vuelve su longitud.
- Lo que cambió, sin observar otra vez: navegación del frame principal, pestaña nueva, diálogo abierto y si el
  texto de la página cambió (una huella, que es también el estado del detector de bucles). La fuente de verdad
  sigue siendo el siguiente `browser_observe`.

Autocomprobación:  uv run --project . tests/test_act.py
"""

import asyncio
import hashlib
import json
import re

from playwright.async_api import TimeoutError as PlaywrightTimeout

import refs
from browser import forms, observe, sanitize
from refs import StaleHandle

SETTLE = 0.3  # s tras actuar antes de mirar qué cambió: una navegación o un diálogo arrancan en ese margen
NAV_WAIT = 10.0  # s que se espera el domcontentloaded de una navegación que provocó la acción
CHOOSER_WAIT = 5.0  # s que se espera el selector de ficheros tras pulsar el botón de una subida
NOT_TEXT = {"checkbox", "radio", "file", "select", "button", "submit", "reset", "image", "range", "color"}
# Pegar mete el portapapeles de Diego en la página: ni lo que tenga, ni a ciegas.
PASTE = re.compile(r"^(?:(?:Control|ControlOrMeta|Meta)\+(?:Shift\+)?v|Shift\+Insert)$", re.I)


class Refused(RuntimeError):
    """Campo secreto: lo escribe Diego. `next` va con la excepción (el `Refused` del escritorio dice otra cosa)."""

    next = "Pásaselo a Diego con browser_handoff(reason) y, cuando él diga que ha terminado, browser_resume."


class Occluded(RuntimeError):
    """Algo tapa el elemento; el mensaje dice qué."""


# La identidad del nodo y lo que hace falta para decidir sobre él, sin valores.
IDENT = "(a => {\n" + observe.XPATH + "\n" + observe.NAME + r"""
  const el = (globalThis.__MANOS_NODES || [])[a.i];
  if (!el || !el.isConnected || xpath(el) !== a.xp || nameOf(el)[0] !== a.n) return {stale: 1};
  return {k: el.tagName === 'INPUT' ? el.type : el.tagName.toLowerCase(),
          ac: el.getAttribute('autocomplete') || '', multi: el.multiple ? 1 : 0,
          ed: (/^(INPUT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable) ? 1 : 0,
          host: location.host, top: window === top ? 1 : 0};
})"""
SCROLL = forms._HEAD + " el.scrollIntoView({block: 'center', behavior: 'instant'}); return {ok: 1}; })"
TEXT = "document.body ? document.body.innerText.slice(0, 200000) : ''"


# Adónde apunta un ref, para browser_download: la imagen (él mismo o la primera de dentro) o el enlace que lo contiene.
SRC = forms._HEAD + r"""
  const img = el.tagName === 'IMG' ? el : el.querySelector('img');
  if (img) return {u: img.currentSrc || img.src || ''};
  const ln = el.closest('a[href]');
  return {u: ln && typeof ln.href === 'string' ? ln.href : ''};
})"""


def _stale(ref_id: int) -> StaleHandle:
    return StaleHandle(f"[{ref_id}] ya no apunta a lo observado (la página navegó o cambió): vuelve a llamar a "
                       "browser_observe.")


async def _ident(cdp, ref_id: int, ref: refs.Ref) -> dict:
    if ref.world == 0 or observe._worlds.get(ref.frame_id) != ref.world:
        raise _stale(ref_id)
    try:
        got = await cdp.send("Runtime.evaluate", {
            "expression": f"{IDENT}({json.dumps({'i': ref.i, 'xp': ref.xpath, 'n': ref.name})})", "contextId": ref.world,
            "returnByValue": True})
    except Exception:
        raise _stale(ref_id) from None  # el contexto murió: navegó
    info = got.get("result", {}).get("value") or {}
    if "exceptionDetails" in got or info.get("stale"):
        raise _stale(ref_id)
    return info


def _files(paths: list[str], multi: bool) -> list[str]:
    """`forms.safe_file` por ruta (otra vez dentro de `_set_files`: es su única puerta). Un input sin `multiple` se
    queda con el primero de dos EN SILENCIO: se rechaza aquí."""
    if not paths:
        raise ValueError("upload pide `files`: una o más rutas absolutas.")
    if len(paths) > 1 and not multi:
        raise ValueError("Ese campo admite un solo fichero (no tiene `multiple`).")
    return [forms.safe_file(p) for p in paths]


async def _digest(session) -> str | None:
    """Huella del texto de la página: «¿cambió algo?» sin observar. None si no se puede leer (diálogo, navegando)."""
    if session.dialog is not None:
        return None
    try:
        text = await asyncio.wait_for(session.page.evaluate(TEXT), 2.0)
    except Exception:
        return None
    return hashlib.sha256((session.page.url + "\0" + text).encode()).hexdigest()[:16]


async def link(session, ref_id: int) -> str:
    """La url de la imagen o del enlace de un ref (`SRC`), con la misma identidad que `act`. **Quien llama tiene
    `session.exclusive()`.** No sale al modelo: la usa browser_download."""
    session.require_no_dialog()
    ref = refs.lookup(ref_id)
    if ref.frame_id.startswith("atspi:"):
        raise ValueError(f"[{ref_id}] es un ref del escritorio (ui_snapshot), no de la página.")
    async with forms.Frames(session) as frames:
        cdp = frames.cdp.get(ref.frame_id)
        if cdp is None:
            raise _stale(ref_id)
        await _ident(cdp, ref_id, ref)
        got = await forms._eval1(cdp, ref.frame_id, SRC, {"i": ref.i})
    if got.get("gone") or got.get("detached"):
        raise _stale(ref_id)
    if not got.get("u"):
        raise ValueError(f"[{ref_id}] no es ni imagen ni enlace: usa el ref de la miniatura o del enlace.")
    return got["u"]


async def act(session, ref_id: int, action: str, value: str = "", keys: str = "",
              files: list[str] | None = None) -> dict:
    """Un verbo sobre un ref. **Quien llama tiene `session.exclusive()`.**"""
    session.require_no_dialog()
    ref = refs.lookup(ref_id)
    if ref.frame_id.startswith("atspi:"):
        raise ValueError(f"[{ref_id}] es un ref del escritorio (ui_snapshot): se usa con ui_act, no con browser_act.")
    if action == "press" and PASTE.match(keys.replace(" ", "")):
        raise Refused("Pegar no: metería el portapapeles de Diego en la página. Escribe el texto con type.")
    page, before = session.page, await _digest(session)
    pages, url0 = len(session.context.pages), page.url
    navs: list[int] = []
    on_nav = lambda f: navs.append(1) if f is page.main_frame else None
    readback: dict = {}
    async with forms.Frames(session) as frames:
        cdp, fid, i = frames.cdp.get(ref.frame_id), ref.frame_id, ref.i
        if cdp is None:
            raise _stale(ref_id)
        info = await _ident(cdp, ref_id, ref)
        secret = "A" in (ref.sec, sanitize.classify(info)) or (not info["top"] and forms.PAY_HOST.search(info["host"]))
        if secret and action in ("type", "select", "press"):
            raise Refused(f"[{ref_id}] es un campo secreto (contraseña, tarjeta, CVC u OTP): manos no lo escribe.")
        if action == "type" and (info["k"] in NOT_TEXT or not info["ed"]):
            raise ValueError(f"type es para campos de texto y [{ref_id}] es {info['k']}: usa select, check, "
                             "upload o click.")
        if action in ("type", "press"):
            prep = await forms._eval1(cdp, fid, forms.PREP, {"i": i, "clear": action == "type"})
            if prep.get("gone") or prep.get("detached"):
                raise _stale(ref_id)
            if sanitize.classify({"k": prep.get("fk", ""), "ac": prep.get("fac", "")}) == "A":  # antes que `focused`:
                raise Refused(f"El foco de [{ref_id}] acaba en un campo secreto: manos no escribe ahí.")  # delegó
            if not prep["focused"]:
                raise Occluded(f"[{ref_id}] no acepta el foco ({prep.get('covered') or 'no es editable'}).")

        async def verb() -> None:
            if action == "click":
                done = await forms._click(cdp, fid, i)
                if done.get("gone") or done.get("detached"):
                    raise _stale(ref_id)
                if done.get("err"):
                    raise Occluded(f"[{ref_id}]: {done['err']}.")
            elif action == "type":
                if info["k"] in forms.DATEISH:  # aquí no se puede teclear (forms.py, punto 2): ISO por el setter
                    res = await forms._eval1(cdp, fid, forms.SET_NATIVE, {"i": i, "v": value})
                    if res.get("err"):
                        raise ValueError(f"[{ref_id}] no aceptó el valor: {res['err']}")
                else:
                    await cdp.send("Input.insertText", {"text": value})
                got = await forms._eval1(cdp, fid, forms.READBACK, {"i": i})
                readback["len"] = len(str(got.get("v", "")))
            elif action == "press":
                if not keys:
                    raise ValueError("press pide `keys`: Enter, Tab, Escape, ArrowDown, Control+a…")
                try:
                    await page.keyboard.press(keys)
                except Exception as exc:
                    if "Unknown key" not in str(exc):
                        raise
                    raise ValueError(f"Tecla desconocida «{keys}»: usa los nombres de Playwright (Enter, Tab, "
                                     "Escape, ArrowDown, Control+a).") from exc
            elif action == "select":
                if info["k"] != "select":
                    raise ValueError(f"select es para un <select> y [{ref_id}] es {info['k']}: un desplegable "
                                     "propio va con click en él y click en la opción.")
                res = await forms._eval1(cdp, fid, forms.SELECT_OPTION, {"i": i, "v": value})
                if res.get("err"):
                    raise ValueError(f"«{value}» no es una opción de [{ref_id}]. Algunas: {res.get('sample')}")
                readback["selected"] = res["t"]
            elif action in ("check", "uncheck"):
                want = action == "check"
                chk = (await forms._eval1(cdp, fid, forms.READBACK, {"i": i})).get("chk")
                if chk is None:
                    raise ValueError(f"[{ref_id}] no es una casilla: usa click.")
                if bool(chk) != want:
                    done = await forms._click(cdp, fid, i)
                    if done.get("gone") or done.get("detached"):
                        raise _stale(ref_id)
                    if done.get("err"):  # un control repintado (opacity:0 bajo un span): su etiqueta lo reenvía
                        await forms._eval1(cdp, fid, forms.CLICK_LABEL, {"i": i})
                    await asyncio.sleep(forms.SETTLE)
                    chk = (await forms._eval1(cdp, fid, forms.READBACK, {"i": i})).get("chk")
                readback["checked"] = bool(chk)
            elif action == "scroll":
                await forms._eval1(cdp, fid, SCROLL, {"i": i})
            elif action == "upload" and info["k"] == "file":
                await forms._set_files(cdp, fid, i, _files(files or [], bool(info["multi"])))
                readback["files"] = len((await forms._eval1(cdp, fid, forms.READBACK, {"i": i})).get("files", ()))
            elif action == "upload":
                # El ref es el botón de un <input type=file hidden>: se pulsa y se recoge el selector que abre de
                # verdad (Playwright lo intercepta: no llega al portal). Así es el input que el botón abre, no
                # el primero que haya en la página.
                paths = _files(files or [], True)
                try:
                    async with page.expect_file_chooser(timeout=CHOOSER_WAIT * 1000) as chooser:
                        done = await forms._click(cdp, fid, i)
                        if done.get("gone") or done.get("detached"):
                            raise _stale(ref_id)
                        if done.get("err"):
                            raise Occluded(f"[{ref_id}]: {done['err']}.")
                    fc = await chooser.value
                except PlaywrightTimeout:
                    raise ValueError(f"[{ref_id}] no es un campo de ficheros ni abrió un selector al pulsarlo: usa "
                                     "el ref del botón que lo abre.") from None
                if len(paths) > 1 and not fc.is_multiple():
                    raise ValueError("Ese campo admite un solo fichero (no tiene `multiple`).")
                await fc.set_files(paths)
                readback["files"] = await fc.element.evaluate("e => e.files.length")
            else:
                raise ValueError(f"Verbo desconocido «{action}».")

        page.on("framenavigated", on_nav)
        try:
            # Un diálogo nativo (alert en el manejador del clic) bloquea el renderer, y la llamada CDP no vuelve
            # hasta que alguien lo responda: se espera al verbo O al diálogo. Con diálogo, el verbo queda en vuelo
            # y termina cuando browser_dialog lo responde.
            job = asyncio.ensure_future(verb())
            while not job.done() and session.dialog is None:
                await asyncio.sleep(0.05)
            if job.done():
                await job
            else:
                job.add_done_callback(lambda t: t.cancelled() or t.exception())
            await asyncio.sleep(SETTLE)
        finally:
            page.remove_listener("framenavigated", on_nav)

    navigated = bool(navs) or page.url != url0
    new_page = len(session.context.pages) > pages
    if navigated:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_WAIT * 1000)
        except Exception:
            pass  # sigue cargando: lo dirá la siguiente observación
    if navigated or new_page:  # los refs y los ids de campo mueren con la página
        observe.reset()
        forms.reset()
    after = await _digest(session)
    return sanitize.scrub({
        "action": action, "readback": readback, "url": sanitize.clean_url(session.page.url),
        "navigated": navigated, "new_page": new_page,
        "dialog": session.dialog.type if session.dialog is not None else None,
        "changed": None if before is None or after is None else before != after,
        "state": after})
