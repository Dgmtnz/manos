"""La observación por defecto: UNA cosecha JS en mundo aislado, con el hit-test dentro.

Esto no es una preferencia, es el resultado de P0-C medido sobre seis páginas reales, n=10 cada una:

| página            | join AX×DOMSnapshot p50/p95 | cosecha p50/p95 |
|-------------------|-----------------------------|-----------------|
| formulario 24 c.  | 28,7 / 43,0 ms              | **5,9 / 15,1**  |
| Wikipedia         | **3647 / 5567 ms**          | **74,6 / 174,7**|
| news.ycombinator  | 286,1 / **459,2 ms**        | **28,8 / 43,5** |

El join se pasa 13,9x del presupuesto de 400 ms en Wikipedia y lo pasa incluso en HN, que era la página
pequeña de referencia. Y además **pierde en calidad**: contra la verdad de terreno del formulario de
laboratorio el join da precisión 0,553 con 21 falsos positivos de 47 filas, y la cosecha 1,000 con 0.
Así que el join baja a sonda opt-in y `DOMSnapshot.captureSnapshot` sale del camino caliente por
completo, porque la oclusión era la única razón por la que estaba ahí.

Las cuatro cosas que este módulo hace y no vuelve a discutir:

1. **El hit-test de oclusión va DENTRO del mismo `Runtime.evaluate`.** Un `elementFromPoint` en bloque
   cuesta 0,02-0,1 ms por elemento (3,8 ms para los 185 de HN). La heurística de orden de pintado que
   sustituye necesitaba `captureSnapshot` —41,7 ms en HN, 585,9 ms en Wikipedia, más 50-621 ms de
   Python— para dar una respuesta **peor** (borró tres enlaces de navegación reales de una SPA). Así que
   `[covers:N]` es un hecho, no una heurística.
2. **El nombre desciende a `img[alt]`, `[aria-label]` y `title` de `svg` desde el día uno.** Son dos
   líneas y sin ellas 9 de 53 enlaces de una rejilla de producto salen sin etiqueta, y la tentación es
   pagar enriquecimiento AX para arreglar un bug propio. Con el descenso, 52 de 53 nombrados: los mismos
   que nombraba el join.
3. **`xpath` y `href` se quedan en el mapa de refs y no se serializan.** Es lo que pone la cosecha por
   debajo del join en tokens en todas las páginas reales (HN 3092 contra 3564).
4. **Del formulario se cosecha lo que el árbol AX demostrablemente no tiene**: `type`, `pattern`,
   `maxlength`, `min`, `max`, `step`, `inputmode`, el `autocomplete` de HTML, los *valores* de las
   `<option>` y la validez real (`validity`, `checkValidity()`, `validationMessage`). La propiedad AX
   `invalid` está **mal** para inputs de texto —dijo `false` sobre un campo que era `:invalid`— así que
   nunca se decide con ella.
5. **El valor de un campo de texto libre NO se emite nunca; se emite su longitud (`vl`).** El
   estrangulamiento está aquí y no en el clasificador, que fallaba abierto: lo que no reconocía se
   publicaba, y ahora un fallo suyo cuesta una longitud. Texto libre es todo `input` cuyo tipo no sea
   estructural (un tipo nuevo o desconocido cuenta como texto libre: el defecto cierra), más `textarea`
   y `contenteditable`. El valor viaja en `fv` hasta `sanitize.classify_rows`, que lo quita siempre y
   lo aprende solo si la fila está marcada; el de un `type=hidden`, que no emite fila, viaja en `vals`.
   Sí se emite el valor estructural, que no puede ser un secreto: la `option` elegida de un `select`,
   el `chk` de una casilla y `date/time/month/week/color/range/file`. La lectura de vuelta de lo
   tecleado es trabajo del `readback` de `form_fill`, que sabe lo que el servidor escribió.

El mundo aislado se crea por frame y por observación (`Page.createIsolatedWorld`, p50 **0,4 ms**). El
plan añadía `addScriptToEvaluateOnNewDocument(worldName='manos')` para que el mundo se autocree al
navegar; aquí no compra nada, porque la cosecha y el hit-test son un único `evaluate` sin estado entre
llamadas y el `executionContextId` hay que pedirlo igual. Un viaje de 0,4 ms es más barato que llevar la
contabilidad de los `Runtime.executionContextCreated`.

Autocomprobación:  uv run --project . tests/test_observe.py
"""

import json
import re
from urllib.parse import urlsplit

import refs
from browser import sanitize, webmcp

WORLD = "manos"
MAX_REFS = 200  # el presupuesto lo fija el número de dianas con nombre, no el tamaño del DOM

# --- la cosecha ------------------------------------------------------------------------------------
# Un solo `Runtime.evaluate`: descubrimiento, nombre, contrato de formulario y hit-test de oclusión.
# Deja los nodos en `globalThis.__MANOS_NODES` para que la redacción de capturas pueda tapar por índice
# sin volver a buscarlos.

# Una sola definición: `browser/act.py` la reusa para comprobar que el nodo de un ref sigue siendo el observado.
XPATH = r"""  function xpath(el) {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1) {
      let i = 1, s = n.previousElementSibling;
      while (s) { if (s.nodeName === n.nodeName) i++; s = s.previousElementSibling; }
      parts.unshift(n.nodeName.toLowerCase() + '[' + i + ']');
      n = n.parentNode;
      if (n && n.nodeType === 11) { if (!n.host) break; parts.unshift('//'); n = n.host; }
      else if (!n || n.nodeType === 9) break;
    }
    return '/' + parts.join('/');
  }"""

# El nombre accesible, una sola definición: `browser/act.py` comprueba con ella que el ref sigue siendo el observado.
NAME = r"""  const byId = (el, id) => {
    const r = el.getRootNode();
    return (r && r.getElementById) ? r.getElementById(id) : document.getElementById(id);
  };
  const txt = el => el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const resolve = (el, attr) => {
    const v = el.getAttribute && el.getAttribute(attr);
    if (!v) return '';
    const parts = [];
    for (const id of v.split(/\s+/)) { const t = byId(el, id); if (t) parts.push(txt(t)); }
    return parts.join(' ').trim();
  };

  // `aria-labelledby` ANTES de `aria-label`, que es el orden de HTML-AAM y el mismo que sigue
  // `forms.ADDENDUM`: una precedencia por repositorio. Al revés, un campo con los dos atributos tenía
  // un nombre distinto en cada tool —y `sanitize.classify` lee ese nombre, así que tenía dos niveles
  // de sensibilidad—. La otra etiqueta no se pierde: viaja en `o.al`, que es señal del nivel B.
  function nameOf(el) {
    const lb = resolve(el, 'aria-labelledby');
    if (lb) return [lb, 'labelledby'];
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al && al.trim()) return [al.trim(), 'aria-label'];
    if (el.labels && el.labels.length) {
      const t = txt(el.labels[0]);
      if (t) return [t, el.labels[0].htmlFor ? 'labelfor' : 'labelwrapped'];
    }
    if (el.tagName === 'INPUT' && /^(button|submit|reset)$/.test(el.type) && el.value)
      return [el.value, 'value'];
    const role = el.getAttribute && el.getAttribute('role');
    if (/^(BUTTON|A|SUMMARY)$/.test(el.tagName) || /^(button|link|menuitem|tab|option)$/.test(role || '')) {
      const t = txt(el);
      if (t) return [t.slice(0, 120), 'contents'];
    }
    // El descenso que vale 9 etiquetas de 53 en una rejilla de producto. Sin esto parece que haga
    // falta enriquecimiento AX, y lo que hace falta es esto.
    const im = el.querySelector && el.querySelector('img[alt]:not([alt=""]),[aria-label],svg title');
    if (im) {
      const t = (im.getAttribute && (im.getAttribute('alt') || im.getAttribute('aria-label'))) || txt(im);
      if (t && t.trim()) return [t.trim().slice(0, 120), 'descendant-alt'];
    }
    if (el.placeholder) return [el.placeholder, 'placeholder'];
    const ti = el.getAttribute && el.getAttribute('title');
    if (ti) return [ti, 'title'];
    if (el.tagName === 'IMG' && el.alt) return [el.alt, 'alt'];
    // En un `contenteditable` el texto ES el valor: nombrarlo por su contenido publicaría el secreto
    // por la puerta del nombre. Sin etiqueta se queda sin nombre, que es lo correcto.
    const t2 = el.isContentEditable ? '' : txt(el);
    if (t2 && t2.length < 60) return [t2, 'contents'];
    return ['', ''];
  }"""

HARVEST = r"""(cfg => {
  const TAG_ROLE = {A:'link',BUTTON:'button',SELECT:'combobox',TEXTAREA:'textbox',SUMMARY:'button',
    DETAILS:'group',OPTION:'option'};
  const TYPE_ROLE = {checkbox:'checkbox',radio:'radio',button:'button',submit:'button',reset:'button',
    image:'button',range:'slider',number:'spinbutton',search:'searchbox',file:'button',color:'button',
    date:'textbox',time:'textbox','datetime-local':'textbox',month:'textbox',week:'textbox',
    email:'textbox',tel:'textbox',url:'textbox',password:'textbox',text:'textbox',hidden:null};
  const SEL = 'a[href],button,input,select,textarea,summary,[role],[tabindex],[onclick],[contenteditable]';

  // XPath absoluto cualificado por shadow root: es la parte duradera del ref, y se queda en el servidor.
""" + XPATH + r"""

""" + NAME + r"""

  function roleOf(el) {
    const r = el.getAttribute && el.getAttribute('role');
    if (r) return r.split(/\s+/)[0];
    if (el.tagName === 'INPUT') return TYPE_ROLE[el.type] !== undefined ? TYPE_ROLE[el.type] : 'textbox';
    if (el.tagName === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (el.isContentEditable) return 'textbox';
    return TAG_ROLE[el.tagName]
      || (el.hasAttribute('onclick') || el.hasAttribute('tabindex') ? 'generic-clickable' : null);
  }

  // Un tipo de `input` es ESTRUCTURAL si su valor no puede ser un secreto: una casilla, una fecha, un
  // color, la etiqueta de un botón. Todo lo demás —incluido un tipo nuevo o mal escrito, que el DOM
  // refleja como `text`, e incluido `hidden`, que es donde las máscaras de tarjeta guardan los dígitos
  // crudos— es texto libre, y de un campo de texto libre nunca sale el valor. El defecto cierra.
  const STRUCT = new Set(['checkbox','radio','button','submit','reset','image','file','range','color',
    'date','time','datetime-local','month','week']);
  // De estos tres el `value` ES el nombre accesible, así que emitirlo lo imprime dos veces.
  const NAMED = new Set(['submit','reset','image']);
  const freeValue = el => {
    if (el.isContentEditable) return txt(el);
    if (el.tagName === 'TEXTAREA') return el.value;
    if (el.tagName !== 'INPUT' || STRUCT.has(el.type)) return null;  // null = no es texto libre
    return el.value;
  };

  function* walk(root) {
    for (const el of (root.querySelectorAll ? root.querySelectorAll(SEL) : [])) yield el;
    for (const el of (root.querySelectorAll ? root.querySelectorAll('*') : []))
      if (el.shadowRoot) yield* walk(el.shadowRoot);
  }

  const vw = innerWidth, vh = innerHeight;
  const out = [], nodes = (globalThis.__MANOS_NODES = []);
  const vals = [], seen = new Set();
  let hidden = 0, offvp = 0;

  for (const el of walk(document)) {
    if (seen.has(el)) continue;
    seen.add(el);
    // Un `type=hidden` nunca emite fila, así que nunca se clasifica: su valor se aprende aquí, que es
    // donde las máscaras de tarjeta dejan los dígitos crudos. 8 KiB tapa una clave PEM; más que eso ya
    // no es una credencial, es prosa, y memorizarla costaría una pasada por respuesta.
    const free = freeValue(el);
    if (free && el.type === 'hidden') vals.push(free.slice(0, 8192));
    const role = roleOf(el);
    if (!role) continue;
    if (role === 'generic-clickable') {
      const ti = el.getAttribute('tabindex');
      if (!el.hasAttribute('onclick') && (ti === null || ti === '-1')) continue;
    }
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (!(cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.01
          && r.width > 2 && r.height > 2)) { hidden++; continue; }
    const inVp = r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh;
    if (!inVp) { offvp++; if (cfg.viewport_only) continue; }

    const [n] = nameOf(el);
    const o = {i: nodes.length, r: role, xp: xpath(el)};
    if (n) o.n = n;
    // No se emite: es la segunda señal del nivel B del saneado, y se lee cuando el nombre accesible
    // se lo llevó el `aria-labelledby`.
    const al2 = el.getAttribute && el.getAttribute('aria-label');
    if (al2 && al2.trim() && al2.trim() !== n) o.al = al2.trim();
    if (el.disabled) o.dis = 1;
    if (el.getRootNode() !== document) o.shadow = 1;
    if (el.id) o.id = el.id;    // no se emite: es una de las cuatro señales del nivel B del saneado
    if (el.placeholder) o.ph = el.placeholder;

    if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable) {
      o.k = el.tagName === 'INPUT' ? el.type : el.tagName.toLowerCase();
      if (el.name) o.nm = el.name;
      if (el.type === 'checkbox' || el.type === 'radio') o.chk = el.checked ? 1 : 0;
      else if (free !== null) { o.vl = free.length; if (free) o.fv = free.slice(0, 8192); }
      else if (el.value && !NAMED.has(el.type)) o.v = String(el.value).slice(0, 120);  // estructural
      if (el.required) o.req = 1;
      if (el.readOnly) o.ro = 1;
      if (el.multiple) o.multi = 1;
      // Lo que el árbol AX no tiene y hace falta para rellenar bien un formulario.
      for (const a of ['pattern','minlength','maxlength','min','max','step','inputmode','autocomplete','accept']) {
        const av = el.getAttribute && el.getAttribute(a);
        if (av !== null && av !== undefined && av !== '') o[a === 'autocomplete' ? 'ac' : a] = av;
      }
      if (el.tagName === 'SELECT') {
        o.opts = [...el.options].slice(0, 30).map(op => [op.value, op.text.trim().slice(0, 40)]);
        if (el.options.length > 30) o.opts_n = el.options.length;
      }
      // La verdad sobre la validez es ésta, no la propiedad `invalid` del AX, que está mal para texto.
      if (el.willValidate) {
        const ok = el.checkValidity();
        o.valid = ok ? 1 : 0;
        if (!ok) {
          const why = [];
          for (const k in el.validity) if (k !== 'valid' && el.validity[k]) why.push(k);
          o.why = why.join(',');
          o.msg = el.validationMessage;
        }
      }
    }
    const desc = resolve(el, 'aria-describedby');
    if (desc) o.desc = desc.slice(0, 160);
    // Solo `aria-expanded`: es el único que `_line` imprime. `aria-invalid` además es la propiedad con
    // la que PLAN §5 prohíbe decidir la validez, que se lee de `checkValidity()` arriba.
    const ex = el.getAttribute && el.getAttribute('aria-expanded');
    if (ex !== null && ex !== undefined) o.expanded = ex;
    if (el.tagName === 'A' && el.href) o.href = el.href.slice(0, 300);
    out.push(o); nodes.push(el);
  }

  // Hit-test real, en bloque, en el mismo viaje. `[covers:N]` deja de ser una heurística.
  const index = new Map(nodes.map((el, k) => [el, k]));
  for (let k = 0; k < nodes.length; k++) {
    const el = nodes[k], r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx >= vw || cy >= vh) { out[k].nohit = 1; continue; }
    const root = el.getRootNode();
    const h = (root.elementFromPoint ? root : document).elementFromPoint(cx, cy);
    if (h === el || el.contains(h) || (h && h.contains(el))) continue;
    if (!h) { out[k].cov = 'nada'; continue; }
    let a = h, by = null;
    while (a && by === null) { if (index.has(a)) by = index.get(a); a = a.parentElement; }
    out[k].cov = by === null ? h.tagName.toLowerCase() + (h.id ? '#' + h.id : '') : by;
  }

  return {url: location.href, title: document.title,
          vp: [vw, vh], scroll: [Math.round(scrollX), Math.round(scrollY)],
          skipped_hidden: hidden, skipped_offvp: offvp, vals: vals, els: out,
          consent: document.documentElement.getAttribute('data-manos-consent')};
})"""


_worlds: dict[str, int] = {}   # frame_id -> executionContextId del mundo aislado 'manos'
_secrets: dict[str, list[int]] = {}  # frame_id -> índices de __MANOS_NODES que son secretos


def reset() -> None:
    """Olvida los refs y los mundos: al navegar o al cerrar, nada de esto sigue siendo cierto. Sin retirar los
    refs, el `[1]` de la página anterior sigue resolviendo a un xpath que casualmente existe en la nueva.

    Los secretos memorizados NO: un valor visto en una página puede salir en la siguiente (un eco, un resumen).
    Los olvida `browser_close`."""
    for frame_id in _worlds:
        refs.forget(frame_id)
    _worlds.clear()
    _secrets.clear()


async def _eval(cdp, frame_id: str, expression: str):
    """Evalúa en el mundo aislado del frame, creándolo la primera vez y recreándolo si murió.

    El mundo se cachea porque `__MANOS_NODES` tiene que sobrevivir de la cosecha a la captura: una
    segunda `createIsolatedWorld` devuelve un contexto distinto y vacío. Una navegación limpia los
    contextos y el `evaluate` falla; ese fallo **es** la señal de invalidación, así que no hace falta
    llevar la contabilidad de `Runtime.executionContextsCleared` para esto.
    """
    for attempt in (1, 2):
        ctx = _worlds.get(frame_id)
        if ctx is None:
            world = await cdp.send("Page.createIsolatedWorld",
                                   {"frameId": frame_id, "worldName": WORLD})
            ctx = _worlds[frame_id] = world["executionContextId"]
        try:
            got = await cdp.send("Runtime.evaluate", {
                "expression": expression, "contextId": ctx,
                "returnByValue": True, "awaitPromise": True})
        except Exception:
            _worlds.pop(frame_id, None)
            if attempt == 2:
                raise
            continue
        if "exceptionDetails" in got:
            # Son 300 caracteres de contenido de la página, así que pasan por el estrangulamiento:
            # este mensaje acaba en el modelo por la vía del error.
            raise RuntimeError("La cosecha falló dentro de la página: "
                               + sanitize.scrub(json.dumps(got["exceptionDetails"])[:300]))
        return got["result"].get("value")


async def _frame_sessions(session) -> list[tuple[object, dict, bool]]:
    """`[(cdp, árbol de frames, hay_que_soltarla)]`, una entrada por proceso de renderizado.

    `Page.getFrameTree` es **por proceso**: con un OOPIF presente no devuelve su `childFrames`, así que
    el árbol real es la unión sobre todas las sesiones adjuntas. Playwright ya hace el baile de adjuntar
    una sesión a cada OOPIF, así que se le pide una sesión por frame y se deduplica por el id del frame
    raíz: los iframes en proceso devuelven el árbol de la página y se descartan solos.

    La deduplicación es por id de frame y no por url. Había un pre-filtro por url que se ahorraba los
    12,0 ms p50 de crear y soltar la sesión de un iframe en proceso, pero dos frames con la misma url
    —`about:blank`, que es como empieza todo iframe de carga diferida— hacían invisible al segundo y a
    todas sus dianas. El id del frame raíz no se puede saber sin pedir la sesión, así que el precio del
    acierto son esos 12 ms por iframe del mismo proceso.
    """
    page = session.page
    main = await session.cdp(page)
    tree = await main.send("Page.getFrameTree")
    out = [(main, tree["frameTree"], False)]
    roots = {tree["frameTree"]["frame"]["id"]}
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            cdp = await session.context.new_cdp_session(frame)
            sub = await cdp.send("Page.getFrameTree")
        except Exception:
            continue  # un frame que se fue mientras se observaba no es un error de la observación
        if sub["frameTree"]["frame"]["id"] in roots:
            await cdp.detach()
            continue
        roots.add(sub["frameTree"]["frame"]["id"])
        out.append((cdp, sub["frameTree"], True))
    return out


def _consent(got: dict) -> dict:
    """El banner de cookies que `browser/consent.js` rechazó en este frame. Lo apunta en un atributo del DOM que la
    página puede escribir: solo pasa un nombre corto en minúsculas, nunca texto suyo."""
    cmp = got.get("consent")
    return {"consent": f"rechazado ({cmp})"} if isinstance(cmp, str) and re.fullmatch(r"[a-z]{1,15}", cmp) else {}


async def _page_tools(cdp, frame_id: str, url: str, main: str) -> dict:
    """Las tools WebMCP que declara el frame (browser/webmcp.py), o nada: en una página normal no cuesta tokens. Solo
    el principal y los de su origen: la tool de un iframe de anuncios no se ofrece."""
    if not webmcp.same_origin(url, main):
        return {}
    try:
        tools = webmcp.describe(await _eval(cdp, frame_id, webmcp.LIST))
    except RuntimeError:
        return {}  # getTools rechazado (p. ej. con document.domain): detectar nunca rompe la observación
    return {"page_tools": tools} if tools else {}


def _flatten(node: dict) -> list[dict]:
    frames = [node["frame"]]
    for child in node.get("childFrames", ()):
        frames += _flatten(child)
    return frames


def _line(ref_id: int, row: dict, covers: dict[int, int]) -> str:
    """Una fila, en el formato compacto de PLAN §4.2. Sin xpath, sin href y sin cajas: nada de eso lo
    puede usar el modelo, y todo cuesta tokens en cada paso."""
    parts = [f'[{ref_id}] {row["r"]} "{row.get("n", "")}"']
    if row.get("frame"):
        parts.append(f'(iframe: {row["frame"]})')
    if row.get("shadow"):
        parts.append("(shadow)")
    cov = row.get("cov")
    if cov is not None:
        # Invierte el `[covers:ID]` de PLAN §4.2 a propósito: la anotación va sobre el elemento TAPADO
        # y nombra a quien lo tapa, porque el que tapa casi nunca es un candidato interactivo (en
        # mediamarkt era el contenedor de un interstitial de consentimiento) y entonces no hay fila
        # donde colgarla. El id del ocluyente, si es una diana, viene igual.
        parts.append(f"[tapado:{covers.get(cov, cov) if isinstance(cov, int) else cov}]")
    if row.get("nohit"):
        parts.append("[fuera del viewport]")
    if row.get("sec"):
        parts.append(f"[secreto:{row['sec']}]")
    for flag, tag in (("dis", "disabled"), ("req", "required"), ("ro", "readonly"),
                      ("multi", "multiple")):
        if row.get(flag):
            parts.append(f"[{tag}]")
    if "chk" in row:
        parts.append("[checked]" if row["chk"] else "[unchecked]")
    if row.get("expanded") is not None:
        parts.append(f"[expanded:{row['expanded']}]")
    if "vl" in row:
        # Campo de texto libre: la longitud, nunca el contenido. Para decidir, el modelo necesita el
        # largo y la validez (`valid`/`why`, abajo); lo que escribió se lo devuelve `form_fill`.
        parts.append(f"len={row['vl']}")
    elif "v" in row:
        parts.append(f'= "{row["v"]}"')
    if row.get("k") and row["k"] != row["r"]:
        parts.append(f"({row['k']})")
    for key in ("pattern", "maxlength", "minlength", "min", "max", "step", "inputmode", "ac"):
        if key in row:
            parts.append(f"{key}={row[key]}")
    if "opts" in row:
        shown = ", ".join(f"{v}={t}" for v, t in row["opts"][:12])
        parts.append(f"opts[{row.get('opts_n', len(row['opts']))}]: {shown}")
    if row.get("valid") == 0:
        parts.append(f"!invalid({row.get('why', '')}): {row.get('msg', '')}")
    if row.get("desc"):
        parts.append(f"desc: {row['desc']}")
    return "  ".join(parts)


async def observe(session, find: str = "", viewport_only: bool = True,
                  max_refs: int = MAX_REFS) -> dict:
    """La observación por defecto: acuña refs y devuelve el listado compacto, ya saneado.

    **El llamador tiene que tener cogido `session.exclusive()`**: dos observaciones solapadas se
    retiran los refs la una a la otra y la primera devuelve un listado entero que ya no resuelve.
    """
    session.require_no_dialog()
    page = session.page
    lines: list[str] = []
    per_frame: list[dict] = []
    secrets = hidden = offvp = 0
    budget = max_refs  # global, no por frame: 20 iframes no pueden acuñar 20 presupuestos de refs

    sessions = await _frame_sessions(session)
    try:
        for cdp, tree, _own in sessions:
            for frame in _flatten(tree):
                harvest = await _eval(cdp, frame["id"],
                                      f"{HARVEST}({json.dumps({'viewport_only': viewport_only})})")
                rows = harvest["els"]
                sanitize.remember(harvest["vals"])   # los `type=hidden`, que no emiten fila
                secrets += sanitize.classify_rows(rows)
                # Se tapa TODO campo de texto libre, no solo los que el clasificador marcó: una
                # imagen es contexto para el modelo igual que el texto, y el listado ya suprime el
                # valor de todos ellos (emite `vl`, la longitud). Taparlos solo cuando `sec` dejaba
                # los no clasificados legibles en la captura, que era la misma fuga por otra ruta.
                # El precio, deliberado: el modelo pierde la relectura visual de lo que teclea, que
                # es trabajo de la relectura de `form_fill`, donde el servidor sabe lo que escribió.
                _secrets[frame["id"]] = [r["i"] for r in rows if r.get("sec") or "vl" in r]
                hidden += harvest["skipped_hidden"]
                offvp += harvest["skipped_offvp"]
                host = urlsplit(frame.get("url", "")).netloc
                sub = frame["id"] != tree["frame"]["id"] or _own
                # El ref no lleva backendNodeId: el XPath es la parte duradera (PLAN §4.3) y resolver
                # backendNodeIds cuesta un `DOM.requestNode` por elemento (185 viajes en HN).
                # `browser/act.py` resuelve por XPath; si mide que necesita el backendNodeId, que lo
                # pida de uno en uno justo antes de actuar.
                ids, omitted = refs.register(
                    frame["id"],
                    [refs.Ref(frame["id"], r["xp"], r["r"], r.get("n", ""), r.get("href", ""),
                              r["i"], _worlds.get(frame["id"], 0), r.get("sec", ""), harvest["url"])
                     for r in rows], budget)
                budget -= len(ids)
                covers = {r["i"]: i for r, i in zip(rows, ids)}
                for row, ref_id in zip(rows, ids):
                    if sub and host:
                        row["frame"] = host
                    lines.append(_line(ref_id, row, covers))
                per_frame.append({"frame": host or "(principal)", "n": len(ids), "omitted": omitted,
                                  "url": sanitize.clean_url(harvest["url"]),
                                  "scroll": harvest["scroll"], "viewport": harvest["vp"], **_consent(harvest),
                                  **await _page_tools(cdp, frame["id"], harvest["url"], page.url)})
    finally:
        for cdp, _tree, own in sessions:
            if own:
                await cdp.detach()
    webmcp.cap_total(per_frame)

    lines = sanitize.scrub(lines)  # antes de `find`: si no, buscar un secreto diría si está en la página
    shown = lines
    if find:
        needle = find.casefold()
        shown = [ln for ln in lines if needle in ln.casefold()]
        # ponytail: subcadena sin plegar acentos. "boton" no encuentra "botón". Si estorba,
        # unicodedata.normalize("NFKD") en los dos lados, una línea.

    return sanitize.scrub({
        "url": sanitize.clean_url(page.url),
        "title": await page.title(),
        "elements": len(lines),
        "shown": len(shown),
        "secrets": secrets,
        "skipped_hidden": hidden,
        "skipped_offviewport": offvp,
        "frames": per_frame,
        "listing": shown,
    })


# --- el texto de la página (mode=text) -------------------------------------------------------------------
# Lo que el listado de acciones no tiene: párrafos, títulos, celdas de una tabla, un resultado calculado. Una línea
# por bloque (una fila de tabla, celdas con « | »), en orden de documento y con los shadow roots al final. Fuera
# siempre lo que puede llevar un valor: campos de formulario (input, textarea, select) y `contenteditable`, igual
# que la cosecha no emite nunca el valor de un campo de texto libre. Lo demás es texto de la página, que sale
# como sale el nombre de un enlace (el techo de `sanitize`, PLAN §7.1-bis), y pasa por el mismo `scrub`.
TEXT = r"""(cfg => {
  const vw = innerWidth, vh = innerHeight, groups = new Map();
  const FIELD = 'script,style,noscript,template,textarea,select,input,[contenteditable=""],[contenteditable="true"]';
  let offvp = 0;
  function* texts(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) yield n;
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) yield* texts(el.shadowRoot);
  }
  const blockOf = el => {
    for (let e = el; e; e = e.parentElement) {
      const d = getComputedStyle(e).display;
      if (!d.startsWith('inline') && d !== 'contents') return e;
    }
    return el;
  };
  for (const t of texts(document)) {
    const s = t.data.replace(/\s+/g, ' ').trim(), el = t.parentElement;
    if (!s || !el || el.closest(FIELD) || el.isContentEditable || !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) { offvp++; if (cfg.viewport_only) continue; }
    const row = el.closest('tr'), key = row || blockOf(el), cell = row && el.closest('td,th');
    let g = groups.get(key);
    if (!g) groups.set(key, g = {h: /^H[1-6]$/.test(key.tagName) ? +key.tagName[1] : 0, parts: [], cell: null});
    if (g.parts.length && (!row || cell === g.cell)) g.parts[g.parts.length - 1] += ' ' + s;
    else g.parts.push(s);
    g.cell = cell;
  }
  const lines = [...groups.values()].map(g => (g.h ? '#'.repeat(g.h) + ' ' : '') + g.parts.join(' | ').slice(0, 2000));
  return {lines: lines.slice(0, cfg.max_lines), more: Math.max(0, lines.length - cfg.max_lines), offvp: offvp,
          consent: document.documentElement.getAttribute('data-manos-consent')};
})"""
MAX_TEXT = 30000  # caracteres de texto por observación: una página larga se lee con `find` o por trozos


async def text(session, find: str = "", viewport_only: bool = True) -> dict:
    """El texto visible de la página, frame a frame (los iframes de otro origen incluidos). No acuña refs ni toca
    los que hay. **Quien llama tiene `session.exclusive()`.**"""
    session.require_no_dialog()
    lines: list[str] = []
    per_frame: list[dict] = []
    offvp = more = 0
    sessions = await _frame_sessions(session)
    try:
        for cdp, tree, own in sessions:
            for frame in _flatten(tree):
                got = await _eval(cdp, frame["id"], f"{TEXT}({json.dumps({'viewport_only': viewport_only, 'max_lines': 1500})})")
                host = urlsplit(frame.get("url", "")).netloc
                if (frame["id"] != tree["frame"]["id"] or own) and got["lines"]:
                    lines.append(f"(iframe: {host or frame.get('url', '')})")
                lines += got["lines"]
                offvp += got["offvp"]
                more += got["more"]
                per_frame.append({"frame": host or "(principal)", "n": len(got["lines"]),
                                  "url": sanitize.clean_url(frame.get("url", "")), **_consent(got),
                                  **await _page_tools(cdp, frame["id"], frame.get("url", ""), session.page.url)})
    finally:
        for cdp, _tree, own in sessions:
            if own:
                await cdp.detach()
    webmcp.cap_total(per_frame)
    # tachar ANTES de `find` (si no, buscar un secreto diría si está) y de cortar (un corte parte el secreto)
    lines = [ln[:400] for ln in sanitize.scrub(lines)]
    shown = [ln for ln in lines if find.casefold() in ln.casefold()] if find else lines
    out, size = [], 0
    for ln in shown:
        size += len(ln) + 1
        if size > MAX_TEXT:
            more += len(shown) - len(out)
            break
        out.append(ln)
    if more:
        out.append(f"… recortado: {more} líneas más. Usa find, o viewport_only y scroll.")
    return sanitize.scrub({"url": sanitize.clean_url(session.page.url), "title": await session.page.title(),
                           "elements": len(lines), "shown": len(out), "secrets": 0, "skipped_hidden": 0,
                           "skipped_offviewport": offvp, "frames": per_frame, "listing": out})


async def screenshot(session, path: str, fmt: str = "jpeg", quality: int = 70) -> dict:
    """Captura con los campos secretos tapados por CSS: tapar, capturar, destapar.

    Necesita una `observe()` previa sobre el mismo documento, porque tapa por índice de
    `__MANOS_NODES`. Una segunda definición de «qué es un secreto», esta vez escrita en JS, es
    exactamente la deriva que el estrangulamiento existe para evitar.

    El bucle de tapado va DENTRO del `try` cuyo `finally` destapa: si falla a mitad, lo ya tapado se
    destapa igual. Y si había secretos que tapar y no se tapó ninguno —el mundo aislado se recreó entre
    la cosecha y la captura, así que `__MANOS_NODES` volvió vacío— esto **falla ruidosamente** en vez de
    devolver una imagen con los secretos a la vista y `redacted_fields: 0` que nadie comprueba.
    """
    sessions = await _frame_sessions(session)
    tagged = 0
    try:
        targets = [(cdp, frame["id"]) for cdp, tree, _own in sessions
                   for frame in _flatten(tree) if _secrets.get(frame["id"])]
        try:
            for cdp, frame_id in targets:
                got = await _eval(cdp, frame_id, sanitize.tag_js(_secrets[frame_id])) or 0
                if not got:
                    raise RuntimeError(
                        f"La redacción no encontró ninguno de los {len(_secrets[frame_id])} campos "
                        "sensibles del frame, así que la captura saldría con ellos a la vista. "
                        "Vuelve a llamar a browser_observe y repite la captura.")
                tagged += got
            data = await session.screenshot(fmt, quality)
        finally:
            for cdp, frame_id in targets:
                await _eval(cdp, frame_id, sanitize.UNTAG_JS)
    finally:
        for cdp, _tree, own in sessions:
            if own:
                await cdp.detach()
    with open(path, "wb") as fh:
        fh.write(data)
    return {"path": path, "bytes": len(data), "redacted_fields": tagged}
