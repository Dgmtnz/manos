"""`browser_extract`: datos de la página abierta a un fichero, a escala, sin que pasen por el contexto del modelo.

Dos fuentes, y en las dos el JS es fijo y del servidor (N1: no hay `browser_exec`); el modelo solo pasa datos:

- `table`: las filas visibles de una `<table>` (con su cabecera como nombres) o los elementos repetidos que
  case un selector CSS (`fields` nombra un sub-selector por campo; sin él, `{"text": …}`). La paginación es un
  botón «siguiente» (selector CSS o ref de `browser_observe`) que se pulsa hasta que desaparece, se desactiva o
  deja de cambiar la tabla.
- `fetch`: un `fetch` dentro de la página (PLAN §4.5), en el mundo aislado, `credentials: 'include'`, sin
  cabeceras añadidas y **sin seguir redirecciones** (una redirección a otro origen ya mandaría las cookies). Solo
  el mismo origen que la página, nunca `file:` ni `data:`. `{page}` en la url pagina desde `start` hasta una
  lista vacía. 429/503: `Retry-After` o espera exponencial, con un total acotado (`WAIT_MAX`); al pasarse,
  `RateLimited` con lo guardado a salvo.

Al modelo vuelve solo `{count, pages, file, sample (3, tachados), resumed_from, next}`.

**Reanudable:** `<file>.state.json` al lado (huella de los argumentos, páginas, registros, bytes del fichero). La
misma llamada otra vez sigue donde quedó y añade; antes de añadir se corta el fichero a los bytes apuntados, así
que una caída entre escribir y apuntar no duplica una página. Otros argumentos contra un estado que existe es un
error, nunca una sobrescritura. Un fichero que ya existe sin estado tampoco se pisa.

`browser_download` (`download`): un fichero o una imagen a disco. http(s) por `context.request` (las cookies del
perfil), `data:` decodificado aquí; nada más. Misma puerta de rutas que `_out`, nunca pisa un fichero.

Autocomprobación:  uv run --project . tests/test_extract.py  ·  tests/test_download.py
"""

import asyncio
import base64
import binascii
import csv
import email.utils
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit

import refs
from browser import forms, observe, sanitize
from browser.session import check, forbidden

WAIT_MAX = 30.0  # s de espera total por 429/503 en una llamada; el candado del navegador sigue cogido mientras
FETCH_TIMEOUT = 30.0  # s por petición
NEXT_WAIT = 10.0  # s que se espera a que la tabla cambie tras pulsar «siguiente»
STABLE = 0.5  # s que la tabla nueva tiene que quedarse igual para darla por pintada (un «Cargando…», un pintado a trozos)
SAMPLE = 3
DOWNLOAD_MAX = 50 * 1024 * 1024  # bytes por descarga


class RateLimited(RuntimeError):
    """El sitio pide esperar más de lo que cabe en una llamada. `next` dice cómo seguir."""

    def __init__(self, msg: str, wait: float, page: int):
        super().__init__(msg)
        self.next = (f"Lo extraído está guardado. Espera ~{wait:.0f} s y vuelve a llamar a browser_extract con los "
                     f"mismos argumentos: sigue en la página {page} sin repetir nada.")


class HttpError(RuntimeError):
    next = "Mira la url con browser_observe o pide a Diego que compruebe la sesión; no reintentes igual."


class PageRepeats(RuntimeError):
    """Una página trae lo mismo que la anterior: el sitio ignora el parámetro de página."""
    next = ("El parámetro de página no hace efecto: mira en browser_observe cómo pagina la propia página (su nombre "
            "exacto, p. ej. `pagina=` en vez de `page=`) y repite con ese parámetro; si no quedó nada guardado, con el "
            "mismo `filename`.")


class StateMismatch(RuntimeError):
    next = ("Para seguir, repite exactamente los argumentos de antes. Para otra extracción, usa otro `filename`. "
            "No borres el fichero ni su .state.json sin preguntar a Diego.")


TABLE = r"""(a => {
  const t = el => el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
  const vis = el => el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  let els;
  try { els = [...document.querySelectorAll(a.sel)]; } catch (e) { return {bad: String(e).slice(0, 200)}; }
  if (!els.length) return {missing: 1};
  const tb = els[0];
  if (tb.tagName === 'TABLE') {
    const all = [...tb.rows].filter(r => r.closest('table') === tb);
    const head = tb.tHead ? [...tb.tHead.rows].pop()
               : (all.length && [...all[0].cells].every(c => c.tagName === 'TH') ? all[0] : null);
    const body = all.filter(r => r !== head && r.parentElement !== tb.tHead && r.parentElement !== tb.tFoot && vis(r));
    return {cols: head ? [...head.cells].map(t) : [], rows: body.map(r => [...r.cells].map(t))};
  }
  const f = Object.entries(a.fields);
  try {
    return {recs: els.filter(vis).map(el => f.length ? Object.fromEntries(f.map(([k, s]) => [k, t(el.querySelector(s))]))
                                                      : {text: t(el)})};
  } catch (e) { return {bad: String(e).slice(0, 200)}; }
})"""

FETCH = r"""(async a => {
  try {
    const r = await fetch(a.u, {credentials: 'include', redirect: 'manual'});
    if (r.type === 'opaqueredirect') return {redir: 1};
    return {s: r.status, ra: r.headers.get('Retry-After'), t: await r.text()};
  } catch (e) { return {err: String(e).slice(0, 200)}; }
})"""


def _out(filename: str) -> Path:
    """La ruta de salida: relativa al directorio de trabajo, fuera de lo vedado, en una carpeta que exista."""
    if not filename.strip():
        raise ValueError("Falta `filename` (p. ej. datos.ndjson o datos.csv).")
    p = Path(os.path.abspath(os.path.expanduser(filename)))
    for cand in (p, p.with_name(p.name + ".state.json")):
        if forbidden(cand):
            raise forms.PathRefused(f"{filename} está en una carpeta vedada.")
    if not p.parent.is_dir():
        raise ValueError(f"La carpeta de {p} no existe.")
    if p.is_dir():
        raise ValueError(f"{p} es una carpeta: hace falta un nombre de fichero.")
    return p


def _rows_to_recs(got: dict) -> list:
    cols, seen = [], set()
    for i, c in enumerate(got.get("cols") or []):
        c = c or f"col{i + 1}"
        while c in seen:
            c += "_"
        seen.add(c)
        cols.append(c)
    recs = []
    for row in got["rows"]:
        if any(row):
            names = cols + [f"col{i + 1}" for i in range(len(cols), len(row))]
            recs.append(dict(zip(names, row)))
    return recs


async def _main_eval(session, expr: str):
    cdp = await session.cdp()
    fid = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
    return await observe._eval(cdp, fid, expr)


async def _read_table(session, selector: str, fields: dict) -> list:
    got = await _main_eval(session, f"{TABLE}({json.dumps({'sel': selector, 'fields': fields})})")
    if got.get("bad"):
        raise ValueError(f"El selector no vale: {got['bad']}")
    if got.get("missing"):
        raise ValueError(f"Nada casa con «{selector}» en la página (el frame principal; los iframes no).")
    return got["recs"] if "recs" in got else _rows_to_recs(got)


def _digest(recs: list) -> str:
    return hashlib.sha256(json.dumps(recs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


async def _next_locator(session, next_button: str):
    """(locator, huella estable) del botón «siguiente». Un ref se traduce a su xpath una vez: el ref muere al
    navegar o al volver a observar, el xpath no."""
    if not next_button.strip().isdigit():
        loc = session.page.locator(next_button).first
        await _not_formish(loc, next_button)
        return loc, next_button
    ref = refs.lookup(int(next_button))
    main = (await (await session.cdp()).send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
    if ref.frame_id != main or "//" in ref.xpath:
        raise ValueError(f"[{next_button}] está en un iframe o un shadow root: pasa un selector CSS del botón.")
    loc = session.page.locator("xpath=" + ref.xpath).first
    await _not_formish(loc, next_button)
    return loc, "xpath=" + ref.xpath


FORMISH = """e => !!e.form || (e.tagName === 'INPUT' && /^(submit|image|reset)$/i.test(e.type))
                || /^(submit|reset)$/i.test(e.getAttribute('type') || '')"""


async def _not_formish(loc, next_button: str) -> None:
    """«Siguiente» se pulsa hasta 1000 veces: un botón de un formulario o de envío (guardar, borrar) no se acepta."""
    if await loc.count() and await loc.evaluate(FORMISH):
        raise ValueError(f"«{next_button}» es un botón de formulario o de envío: browser_extract solo pulsa un "
                         "«siguiente» de paginación. Pasa el selector o el ref de ese.")


async def _click_next(session, loc) -> bool:
    """Pulsa «siguiente» si existe, se ve y está activo. False si no: fin de la paginación."""
    if not await loc.count() or not await loc.is_visible() or await loc.is_disabled() \
            or await loc.get_attribute("aria-disabled") == "true":
        return False
    await _not_formish(loc, "next_button")  # el nodo pudo cambiar al re-pintar
    await loc.click(timeout=5000)
    return True


async def _after_click(session, selector: str, fields: dict, before: str, url0: str) -> list | None:
    """Espera a que la tabla cambie (re-pintado o navegación) y se quede quieta STABLE s: lo primero distinto puede
    ser un «Cargando…» o media página. Una tabla vacía solo vale como final si no llega nada más. None si no cambia
    en NEXT_WAIT s."""
    deadline, empty, seen, since = time.monotonic() + NEXT_WAIT, None, None, 0.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.15)
        if session.page.url != url0:  # navegó: los refs y los ids de campo de antes ya no valen
            observe.reset()
            forms.reset()
            url0 = session.page.url
            try:
                await session.page.wait_for_load_state("domcontentloaded", timeout=NEXT_WAIT * 1000)
            except Exception:
                pass
        try:
            recs = await _read_table(session, selector, fields)
        except Exception:
            continue  # a media navegación la tabla (o el contexto) aún no está
        digest = _digest(recs)
        if not recs:
            empty, seen = recs, None
        elif digest == before:
            seen = None
        elif digest != seen:
            seen, since = digest, time.monotonic()
        elif time.monotonic() - since >= STABLE:
            return recs
    return empty


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _walk(doc, path: str) -> list:
    for k in filter(None, path.split(".")):
        if isinstance(doc, list) and k.isdigit() and int(k) < len(doc):
            doc = doc[int(k)]
        elif isinstance(doc, dict) and k in doc:
            doc = doc[k]
        else:
            keys = f"; hay: {', '.join(list(doc)[:20])}" if isinstance(doc, dict) else ""
            raise ValueError(f"`path` «{path}»: no hay «{k}»{keys}.")
    if not isinstance(doc, list):
        keys = f" Claves: {', '.join(list(doc)[:20])}." if isinstance(doc, dict) else ""
        raise ValueError(f"`path` «{path}» no lleva a una lista de registros.{keys}")
    return doc


def _same_origin(page_url: str, url: str) -> str:
    base = urlsplit(page_url)
    if base.scheme not in ("http", "https"):
        raise ValueError("fetch solo desde una página http(s): abre el sitio con browser_open.")
    full = urljoin(page_url, url)
    got = urlsplit(full)
    if (got.scheme, got.netloc) != (base.scheme, base.netloc):
        raise ValueError(f"fetch solo al mismo origen que la página ({base.scheme}://{base.netloc}); "
                         "ni otros sitios, ni file:, ni data:.")
    return full


async def _fetch(session, url: str, page: int, waited: list) -> list:
    """Una página de la API: reintenta 429/503 dentro del presupuesto; `waited[0]` lleva lo esperado."""
    delay = 1.0
    while True:
        try:
            got = await asyncio.wait_for(_main_eval(session, f"{FETCH}({json.dumps({'u': url})})"), FETCH_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(f"La página {page} no contestó en {FETCH_TIMEOUT:g} s.") from None
        if got.get("err"):
            raise HttpError(f"El fetch de la página {page} falló: {got['err']}")
        if got.get("redir"):
            raise HttpError(f"La url de la página {page} redirige y manos no sigue redirecciones: usa la url final.")
        if got["s"] in (429, 503):
            # nunca menos que la espera exponencial: un Retry-After de 0 o de una fecha pasada daría un bucle sin fin
            wait, delay = max(_retry_after(got.get("ra")) or 0.0, delay), delay * 2
            if waited[0] + wait > WAIT_MAX:
                raise RateLimited(f"HTTP {got['s']} en la página {page}: el sitio pide esperar {wait:.0f} s y ya se "
                                  f"esperaron {waited[0]:.0f} de {WAIT_MAX:g} en esta llamada.", wait, page)
            await asyncio.sleep(wait)
            waited[0] += wait
            check(session)
            continue
        if not 200 <= got["s"] < 300:
            raise HttpError(f"HTTP {got['s']} en la página {page}.")
        try:
            return json.loads(got["t"])
        except ValueError:
            raise ValueError(f"La página {page} no devolvió JSON (empieza por {got['t'][:60]!r}).") from None


def _fp(args) -> str:
    """Huella de los argumentos que definen la extracción (no `max_pages`: subir el tope y seguir es lo normal)."""
    return hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


class _Sink:
    """El fichero de salida y su estado al lado."""

    def __init__(self, out: Path, fp: str):
        self.out, self.state_path, self.fp = out, out.with_name(out.name + ".state.json"), fp
        self.csv = out.suffix.lower() == ".csv"
        self.st = {"fp": fp, "pages": 0, "page": 0, "count": 0, "bytes": 0, "cols": None, "last": ""}
        self.resumed = self.saved = False
        if out.exists() and self.state_path.exists():
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            if st.get("fp") != fp:
                raise StateMismatch(f"{out.name} es de una extracción con otros argumentos ({self.state_path.name}).")
            size = out.stat().st_size
            if size < st["bytes"]:
                raise StateMismatch(f"{out.name} es más corto de lo que dice su estado: alguien lo tocó.")
            os.truncate(out, st["bytes"])  # una página escrita sin apuntar (caída) no se duplica
            self.st, self.resumed, self.saved = st, True, True
        elif out.exists():
            raise StateMismatch(f"{out} ya existe y no es una extracción a medias: no se pisa.")

    def write(self, recs: list) -> None:
        if not self.saved:  # el estado (0 bytes) antes que el fichero: una caída en medio no deja un fichero huérfano
            self.save()
        with open(self.out, "a", encoding="utf-8", newline="") as f:
            if self.csv:
                if any(not isinstance(r, dict) for r in recs):
                    raise ValueError("CSV pide registros con campos (objetos): usa un .ndjson.")
                if self.st["cols"] is None:
                    self.st["cols"] = list(recs[0])
                    csv.writer(f).writerow(self.st["cols"])
                w = csv.DictWriter(f, self.st["cols"], restval="", extrasaction="raise")
                try:
                    w.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                 for k, v in r.items()} for r in recs)
                except ValueError as exc:
                    raise ValueError(f"Un registro trae campos que no están en la cabecera del CSV ({exc}): "
                                     "usa un .ndjson.") from None
            else:
                f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
            f.flush()
            os.fsync(f.fileno())
        self.st["bytes"] = self.out.stat().st_size
        self.st["count"] += len(recs)
        self.st["pages"] += 1

    def save(self) -> None:
        self.saved = True
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.st), encoding="utf-8")
        os.replace(tmp, self.state_path)


def _sample(recs: list) -> list:
    out = []
    for r in sanitize.scrub(recs[:SAMPLE]):
        s = json.dumps(r, ensure_ascii=False)
        out.append(r if len(s) <= 400 else s[:400] + "…")
    return out


async def extract(session, source: str, filename: str, selector: str = "table", fields: dict | None = None,
                  url: str = "", path: str = "", start: int = 1, next_button: str = "", max_pages: int = 50) -> dict:
    """La extracción entera. **Quien llama tiene `session.exclusive()`.**"""
    session.require_no_dialog()
    fields = fields or {}
    out = _out(filename)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(session.page.url))
    new: list = []
    done, note = False, ""
    if source == "fetch":
        if not url:
            raise ValueError("source=fetch pide `url`: la ruta de la API en el mismo sitio, con {page} si pagina.")
        paged = "{page}" in url
        _same_origin(session.page.url, url.replace("{page}", str(start)))  # antes de crear nada
        sink = _Sink(out, _fp(["fetch", url, path, start, origin]))
        p = sink.st["page"] + 1 if sink.st["pages"] else start
        resumed_from = p if sink.resumed else None
        waited = [0.0]
        if sink.st["pages"] and not paged:
            done = True
        for _ in range(0 if done else max_pages):
            check(session)
            recs = _walk(await _fetch(session, _same_origin(session.page.url, url.replace("{page}", str(p))), p,
                                      waited), path)
            if not recs:
                done = True
                break
            if paged and _digest(recs) == sink.st.get("last"):
                if not sink.resumed:  # todo lo escrito es de esta llamada y está mal: fuera, para repetir con el mismo nombre
                    sink.out.unlink(missing_ok=True)
                    sink.state_path.unlink(missing_ok=True)
                raise PageRepeats(f"la página {p} es idéntica a la {p - 1}"
                                  + ("" if sink.resumed else "; no queda nada guardado"))
            sink.write(recs)
            sink.st["page"], sink.st["last"] = p, _digest(recs)
            sink.save()
            new += recs[:SAMPLE - len(new)]
            if not paged:
                done = True
                break
            p += 1
        else:
            note = f"Paró en el tope de {max_pages} páginas por llamada."
    else:
        if url or path:
            raise ValueError("`url` y `path` son de source=fetch; una tabla va con `selector` y `next_button`.")
        loc, key = (await _next_locator(session, next_button)) if next_button else (None, "")
        sink = _Sink(out, _fp(["table", selector, fields, key, origin]))
        resumed_from = sink.st["pages"] + 1 if sink.resumed else None
        click = False
        if sink.st["pages"]:
            if _digest(await _read_table(session, selector, fields)) != sink.st["last"]:
                raise StateMismatch(f"La tabla ya no está en la página donde quedó (la {sink.st['pages']}): vuelve "
                                    "a ella o usa otro `filename` para empezar de cero.")
            click = True
        for _ in range(max_pages):
            check(session)
            if click:
                url0, before = session.page.url, sink.st["last"]
                if loc is None or not await _click_next(session, loc):
                    done = True
                    break
                recs = await _after_click(session, selector, fields, before, url0)
                if recs is None:
                    done, note = True, "«Siguiente» ya no cambia la tabla: se da por el final."
                    break
            else:
                recs = await _read_table(session, selector, fields)
            if not recs:
                done = True
                break
            sink.write(recs)
            sink.st["last"] = _digest(recs)
            sink.save()
            new += recs[:SAMPLE - len(new)]
            click = True
            if loc is None:
                done = True
                break
        else:
            note = f"Paró en el tope de {max_pages} páginas por llamada."
    st = sink.st
    hint = (f"Hecho: {st['count']} registros en {out}. No lo copies al contexto: léelo por trozos solo si hace falta."
            if done else f"{note} Vuelve a llamar con los mismos argumentos para seguir sin repetir.")
    if done and note:
        hint = note + " " + hint
    return {"count": st["count"], "pages": st["pages"], "file": str(out), "sample": _sample(new),
            "resumed_from": resumed_from, "next": hint}



async def download(session, url: str, filename: str) -> dict:
    """`url` (http, https o data:) a `filename`, que no puede existir. **Quien llama tiene `session.exclusive()`.**"""
    out = _out(filename)
    if os.path.lexists(out):
        raise FileExistsError(f"{out} ya existe.")
    scheme = urlsplit(url).scheme.lower()
    if scheme == "data":
        head, comma, payload = url[5:].partition(",")
        if not comma:
            raise ValueError("Url data: sin coma: no hay datos que guardar.")
        ctype = head.split(";")[0].strip() or "text/plain"
        try:
            body = (base64.b64decode(unquote(payload)) if head.lower().endswith(";base64")
                    else unquote_to_bytes(payload))
        except binascii.Error as exc:
            raise ValueError(f"Url data: con base64 roto ({exc}).") from None
    elif scheme in ("http", "https"):
        # ponytail: Playwright lee el cuerpo entero antes de devolver; el tope protege el disco, no la memoria (el
        # plazo lo acota). Si hace falta, descargar en streaming con las cookies de `context.cookies()`.
        resp = await session.context.request.get(url, timeout=60_000, max_redirects=10)
        try:
            if not resp.ok:
                raise HttpError(f"La descarga dio HTTP {resp.status}.")
            size = resp.headers.get("content-length", "")
            if size.isdigit() and int(size) > DOWNLOAD_MAX:
                raise ValueError(f"Pesa {int(size) // 2**20} MB; el tope es {DOWNLOAD_MAX // 2**20} MB.")
            body = await resp.body()
            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
        finally:
            await resp.dispose()
    else:
        raise ValueError(f"Solo http, https o data: (no «{scheme or url[:20]}»).")
    if len(body) > DOWNLOAD_MAX:
        raise ValueError(f"Pesa {len(body) // 2**20} MB; el tope es {DOWNLOAD_MAX // 2**20} MB.")
    try:
        f = open(out, "xb")  # O_EXCL: si alguien lo creó entre medias, no se pisa
    except FileExistsError:
        raise FileExistsError(f"{out} ya existe.") from None
    try:
        with f:
            f.write(body)
    except BaseException:
        out.unlink(missing_ok=True)  # nada a medias
        raise
    return {"saved_to": str(out), "bytes": len(body), "content_type": ctype[:100]}
