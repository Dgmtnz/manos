"""WebMCP: las tools que declara la propia página (`document.modelContext.registerTool`), detectadas y usadas.

Experimental y tras flag (`--enable-features=WebMCP`, browser/session.py), y casi ningún
sitio las declara: se detectan y se usan, nunca se depende de ellas. Solo hay `document.modelContext` en contexto
seguro (https, localhost): en `data:` y en el `about:blank` inicial no existe.

- Solo el frame principal y los de su mismo origen: la tool de un iframe de anuncios no se lista ni se llama.
- Detección: `LIST` en el mundo aislado de cada frame, dentro de `browser_observe` (`frames[i].page_tools`). Solo
  las del propio frame. Nombre, descripción y esquema son texto de la página: con tope por tool y por observación
  (`cap_total`), sin las `description`/`title` de dentro del esquema, y la descripción sale como
  `page_description`, igual de literal que el nombre de un enlace (el techo de `sanitize`).
- Visto bueno: toda tool lo pide (`NeedsApproval`) salvo las de lectura, por el verbo con que empieza su nombre
  (`READ`) o por su `readOnlyHint`. Una lista de verbos peligrosos se quedaba corta (finalizar_compra, checkOut,
  transferir, tramitar…); la de lectura falla cerrada. El `readOnlyHint` lo declara la página: es su palabra.
- Uso: `frame.webmcp.call_tool` de Playwright (`executeTool` en su mundo de utilidad). La salida ya viene en
  bloques de contenido MCP; sale el texto, con tope y tachado.

Autocomprobación:  uv run --project . tests/test_webmcp.py
"""

import json
import re
from urllib.parse import urlsplit

from browser import sanitize

LIST = r"""(async () => {
  const mc = document.modelContext;
  if (!mc || !mc.getTools) return [];
  return (await mc.getTools()).filter(t => !('window' in t) || t.window === window).map(t => ({
    name: String(t.name), description: String(t.description || ''), ro: !!(t.annotations && t.annotations.readOnlyHint),
    schema: typeof t.inputSchema === 'string' ? t.inputSchema : JSON.stringify(t.inputSchema ?? null)}));
})()"""
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_DESC, MAX_TOOL, MAX_TOTAL, MAX_OUT = 300, 2000, 4000, 8000  # caracteres: descripción, tool, observación, salida
TIMEOUT = 15.0  # s: una tool de la página que no contesta no retiene el candado del navegador
READ = re.compile(r"(buscar|ver|consultar|listar|mostrar|obtener|get|search|list|find|show|view)\b")


class NeedsApproval(PermissionError):
    """Una tool de la página que no es de lectura, sin `approved`."""


class NoSuchPageTool(LookupError):
    """La página no declara una tool con ese nombre."""


def _cap(o, depth: int = 4):
    """El esquema de la página, acotado: cadenas, listas, claves y profundidad."""
    if isinstance(o, str):
        return o[:150]
    if depth == 0:
        return None
    if isinstance(o, list):
        return [_cap(v, depth - 1) for v in o[:20]]
    if isinstance(o, dict):
        return {str(k)[:64]: _cap(v, depth - 1) for k, v in list(o.items())[:20] if k not in ("description", "title")}
    return o


def describe(raw) -> list[dict]:
    """Lo que devuelve `LIST`, como sale en `frames[i].page_tools`. Vacío sin tools: cero tokens."""
    out = []
    for t in raw or []:
        if not NAME.fullmatch(t["name"]):
            continue
        try:
            schema = _cap(json.loads(t["schema"]))
        except ValueError:
            schema = None
        entry = {"name": t["name"], "page_description": t["description"][:MAX_DESC], "input_schema": schema}
        if needs_approval(t["name"], t["ro"]):
            entry["needs_approval"] = True
        if len(json.dumps(entry, ensure_ascii=False)) > MAX_TOOL:
            entry["input_schema"] = "(esquema demasiado largo)"
        out.append(entry)
    return out


def validate(schema, data) -> None:
    """Lo mínimo del esquema: un objeto con las claves obligatorias."""
    if not isinstance(data, dict):
        raise ValueError("`input` tiene que ser un objeto JSON con las claves del input_schema de la tool.")
    required = schema.get("required") if isinstance(schema, dict) else None
    missing = [k for k in required or [] if isinstance(k, str) and k not in data]
    if missing:
        raise ValueError(f"Faltan claves obligatorias en `input`: {', '.join(missing)}.")


def cap_total(frames: list[dict]) -> None:
    """Tope de `page_tools` por observación, en orden de frames: lo que no cabe se cuenta en `page_tools_omitted`."""
    room = MAX_TOTAL
    for f in frames:
        tools = f.pop("page_tools", [])
        keep = [t for t in tools if (room := room - len(json.dumps(t, ensure_ascii=False))) >= 0]
        if keep:
            f["page_tools"] = keep
        if len(keep) < len(tools):
            f["page_tools_omitted"] = len(tools) - len(keep)


def needs_approval(name: str, read_only: bool) -> bool:
    words = re.sub(r"[_.-]|(?<=[a-z])(?=[A-Z])", " ", name).lower()  # ver_carrito, getUser
    return not (read_only or READ.match(words))


def same_origin(url: str, main: str) -> bool:
    a, b = urlsplit(url), urlsplit(main)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def _text(out) -> tuple[str, bool]:
    """La salida de la tool: el texto de sus bloques de contenido, o lo que devolviera tal cual."""
    if isinstance(out, dict) and isinstance(out.get("content"), list):
        parts = [c.get("text", "") if c.get("type") == "text" else f"[{c.get('type')}]"
                 for c in out["content"] if isinstance(c, dict)]
        return "\n".join(map(str, parts)), bool(out.get("isError"))
    return (out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)), False


async def call(session, name: str, data, approved: bool = False) -> dict:
    """Llama a la tool `name` de la página. **Quien llama tiene `session.exclusive()`.**"""
    session.require_no_dialog()
    if not NAME.fullmatch(name):
        raise ValueError("`name` tiene que ser el de una de `page_tools` (letras, cifras, _ . -).")
    page = session.page
    found = []
    for frame in page.frames:
        if not same_origin(frame.url, page.url):
            continue
        try:
            tools = await frame.webmcp.tools()
        except Exception:
            continue  # sin modelContext (contexto no seguro) o un frame que se fue
        found += [t["name"] for t in tools]
        tool = next((t for t in tools if t["name"] == name), None)
        if tool:
            break
        # ponytail: si dos frames del mismo origen declaran el mismo nombre gana el primero (el principal).
    else:
        raise NoSuchPageTool(f"La página no declara ninguna tool «{name}»."
                             + (f" Declara: {', '.join(found[:20])}." if found else " No declara ninguna."))
    validate(tool.get("inputSchema"), data)
    if not approved and needs_approval(name, bool((tool.get("annotations") or {}).get("readOnly"))):
        raise NeedsApproval(f"La tool de la página «{name}» no es de lectura (su nombre no empieza por buscar, ver, "
                            "consultar, get… ni se declara readOnlyHint): puede cambiar algo. No se ha llamado.")
    url0 = page.url
    # ponytail: un alert() dentro de la tool la deja colgada hasta TIMEOUT (luego DialogOpen). Si pasa, la carrera
    # contra `session.dialog` de act.act.
    text, err = _text(await frame.webmcp.call_tool(name, data, timeout=TIMEOUT * 1000))
    text = sanitize.scrub(text)  # antes de cortar: un corte parte el secreto
    more = len(text) - MAX_OUT
    text = text[:MAX_OUT] + (f"\n… recortado: {more} caracteres más." if more > 0 else "")
    return {"output": text, "is_error": err, "frame": urlsplit(frame.url).netloc or "(principal)",
            "navigated": page.url != url0}
