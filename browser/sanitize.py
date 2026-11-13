"""El estrangulamiento: nada sale hacia el modelo sin pasar por `scrub()`.

Medido con valores reales en campos reales (PLAN §7.1), que es por lo que este módulo existe:

| llamada CDP                        | contraseña                  | OTP   | nº de tarjeta |
|------------------------------------|-----------------------------|-------|---------------|
| `DOM.getDocument{depth:-1}`        | limpio                      | limpio| limpio        |
| `DOM.getDocument{pierce:true}`     | **FUGA en claro**           | FUGA  | FUGA          |
| `Accessibility.getFullAXTree`      | 14 puntos (filtra el largo) | FUGA  | FUGA          |
| `DOMSnapshot.captureSnapshot`      | **FUGA**                    | FUGA  | FUGA          |
| `DOM.getAttributes`, `getOuterHTML`| limpio                      | limpio| limpio        |

Chromium guarda el texto en claro como `nodeValue` de un `#text` dentro del shadow DOM del input, así
que filtrar por `type in {password, file, hidden}` es insuficiente: un campo de OTP es `type=text`.

**El techo, exacto.** El valor de un campo no sale: la cosecha emite su longitud (`observe.HARVEST`,
`o.vl`). Lo que la página escriba en cualquier otro sitio —un nombre accesible, un `placeholder`, un
`title`, el texto de una `<option>`, un `aria-describedby`, un `validationMessage`, el título de la
página, su propio texto visible— **sale literal**, y `scrub()` no puede taparlo: solo sabe tachar
cadenas que fueron el valor de un campo. Lo único que se recorta aparte son los parámetros de url con
pinta de credencial (`clean_url`). Las rutas que siguen llevando un secreto están contadas en
`tests/test_sanitize.py`, con la aserción escrita para que el número solo pueda bajar.

**Se aprende el valor de los campos marcados (nivel A o B) y de los `input type=hidden`**, que nunca
emiten fila; nada más. Medido: aprenderlo todo tachaba el eco de lo que el agente acababa de escribir
—`link "Más «redactado»"` tras buscar «zapatos de piel»— y sin aprender nada 8 de los 9 valores
tecleados seguían sin salir. Un campo que no emite fila tampoco se clasifica, así que el valor de uno
sensible bajo el pliegue solo se aprende con `viewport_only=False`.

**Una sola clasificación por campo.** El nivel B se decide leyendo las cinco señales de `SIGNALS`, y
las dos cosechas (`observe.HARVEST` y `forms.ADDENDUM`) emiten las cinco con el mismo significado y la
misma precedencia de nombre accesible. Dos cadenas de etiquetado con precedencias opuestas daban dos
niveles de sensibilidad para el mismo campo, y la relectura de `form_fill` usaba el que falla.

**El nivel B tacha y escala, nunca bloquea el tecleo.** Pilla los 9 campos sensibles de la página
adversaria, incluido un `type=text` con `aria-label="Verification code"`, y también un email y un
buscador con «secret» en el placeholder: bloquear dejaría al agente sin escribir en un buscador.

**Un shadow root `closed` es inalcanzable** (`el.shadowRoot` es `null`): ni fila, ni valor aprendido, ni
redacción en la captura. Si la página espeja ese valor a su texto visible, sale.

Autocomprobación:  uv run --project . tests/test_sanitize.py
"""

import json
import re
from urllib.parse import urlsplit, urlunsplit

REDACTED = "«redactado»"
MIN_LEN = 3  # por debajo de esto una cadena aparece por todas partes y tacharla destroza la respuesta
# ponytail: tope conocido — un secreto de 1 o 2 caracteres no se tacha. Si algún día importa, el
# arreglo es tachar solo dentro de los campos que la cosecha marcó, no en toda la respuesta.

# El AX devuelve la contraseña como N viñetas y el largo es un dato, así que la máscara también se
# tacha. Solo viñetas: con `*` dentro, cualquier `****` de cualquier página se llevaba un «redactado».
MASKED = re.compile(r"[•●]{4,}")

# Nivel A: hechos del DOM, no opiniones. Son los tres que la tabla de fugas midió.
HARD_TYPES = frozenset({"password"})
HARD_AUTOCOMPLETE = frozenset({
    "current-password", "new-password", "one-time-code",
    "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
    "cc-name", "cc-given-name", "cc-additional-name", "cc-family-name", "cc-type",
})

# Nivel B: la etiqueta, el aria-label, el name, el id y el placeholder. Tacha y escala; no bloquea.
TIER_B = re.compile(
    r"contrase|password|passwd|\bpwd\b|\bpin\b|\botp\b|one.?time|verificaci|verification"
    r"|\bcvv\b|\bcvc\b|security\s*code|c[oó]digo\s*de\s*seguridad|c[oó]digo\s*de\s*verificaci"
    r"|card\s*n(umber|º)|n[uú]mero\s*de\s*tarjeta|tarjeta\s*de\s*cr[eé]dito|\biban\b|\bswift\b"
    r"|secret|secreto|\btoken\b|api.?key|clave|credencial|credential|passphrase|seed\s*phrase"
    r"|\bssn\b|social\s*security",
    re.IGNORECASE)

# Nombre accesible, `aria-label`, name, id y placeholder — las cinco señales que mide el nivel B.
# `al` está porque el nombre accesible es UNA de las dos etiquetas aria y la cadena del nombre se queda
# con la otra: `aria-labelledby` gana por HTML-AAM, así que un `aria-label="Código de seguridad de la
# tarjeta"` con un `aria-labelledby` inocuo al lado —el patrón exacto de un formulario de pago— no
# llegaba a mirarse. El nivel B es una sospecha sobre el texto que DESCRIBE el campo, y las dos
# etiquetas lo describen: se miran las dos, y así el nivel no depende de qué peldaño ganó la cadena.
SIGNALS = ("n", "al", "nm", "id", "ph")

# Parámetros de url cuyo valor es una credencial. El nombre se conserva: el modelo necesita saber que
# ahí había un `code` para razonar sobre el flujo, y el valor no le sirve de nada.
CRED_PARAMS = frozenset({"code", "token", "access_token", "id_token", "refresh_token", "state",
                         "session", "auth", "key", "password", "pwd", "secret", "sig", "signature"})


def classify(row: dict) -> str:
    """Nivel de sensibilidad de una fila de la cosecha: `"A"`, `"B"` o `""`.

    `A` es un hecho (tipo del input o token de `autocomplete`); `B` es una sospecha por texto, que
    tacha y escala pero nunca impide escribir.
    """
    if row.get("k") in HARD_TYPES:
        return "A"
    tokens = str(row.get("ac", "")).lower().split()
    if any(t in HARD_AUTOCOMPLETE for t in tokens):
        return "A"
    for key in SIGNALS:
        value = row.get(key)
        if value and TIER_B.search(str(value)):
            return "B"
    return ""


# Memoria del servidor y de nadie más. Dict y no set porque el orden de inserción es el desalojo:
# `scrub()` cuesta una pasada por valor en CADA respuesta, así que el conjunto tiene tope.
# ponytail: tope conocido — pasados MAX_KNOWN valores, el más viejo deja de tacharse. `browser_close`
# limpia (navegar ya no); si algún día hacen falta más, el arreglo es subir el número, no quitar el tope.
MAX_KNOWN = 512
_known: dict[str, None] = {}


def remember(values) -> None:
    """Memoriza valores para tacharlos después, estén clasificados o no."""
    for value in values:
        if isinstance(value, str) and len(value) >= MIN_LEN:
            _known.pop(value, None)
            _known[value] = None
    while len(_known) > MAX_KNOWN:
        del _known[next(iter(_known))]


def forget() -> None:
    _known.clear()


def known() -> frozenset[str]:
    return frozenset(_known)


def classify_rows(rows: list[dict]) -> int:
    """Marca las filas sensibles con su nivel y memoriza el valor **solo** de las marcadas.

    El valor de un campo de texto libre llega aquí en `fv` y se quita siempre: no se imprime nunca, y
    aprenderlo cuando el campo no está marcado tacha después la diana que el agente fue a buscar.
    """
    found = 0
    for row in rows:
        tier = classify(row)
        free = row.pop("fv", None)
        if not tier:
            continue
        row["sec"] = tier
        found += 1
        value = free or row.pop("v", None)   # texto libre, o el valor estructural de un campo marcado
        if value:
            remember([str(value)])
    return found


def scrub(obj, secrets: frozenset[str] | set[str] | None = None):
    """**El estrangulamiento.** Toda respuesta pasa por aquí antes de serializarse.

    Devuelve una copia con cada aparición de un valor conocido —y cada máscara de puntos, que filtra la
    longitud— sustituida por `REDACTED`. Sin valores conocidos no hace nada y no cuesta nada.
    """
    secrets = _known if secrets is None else secrets
    if isinstance(obj, str):
        for secret in secrets:
            if len(secret) >= MIN_LEN and secret in obj:
                obj = obj.replace(secret, REDACTED)
        return MASKED.sub(REDACTED, obj)
    if isinstance(obj, dict):
        return {k: scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, secrets) for v in obj]
    return obj


def clean_url(url: str) -> str:
    """La url como la ve el modelo: un `?code=` de OAuth o un token de restablecimiento salen con su
    nombre de parámetro y sin valor, en la query y en el fragmento. El resto de la url no se toca."""
    def params(text: str) -> str:
        out = []
        for part in text.split("&"):
            name, sep, _value = part.partition("=")
            low = name.strip().lower()
            out.append(f"{name}={REDACTED}" if sep and (low in CRED_PARAMS
                                                        or low.endswith("_token")) else part)
        return "&".join(out)

    u = urlsplit(url)
    return urlunsplit(u._replace(query=params(u.query), fragment=params(u.fragment)))


# --- redacción en capturas --------------------------------------------------------------------------
# CSS en vez de cajas post-procesadas: una caja calculada en Python se desincroniza del scroll y del
# layout entre que se mide y se captura. Medido: 23 ms sobre una captura de 143 ms.
# Va en el atributo `style` de cada nodo y no en una hoja del documento porque una hoja inyectada **no
# cruza un shadow root**, y el campo IBAN del formulario de laboratorio vive justo ahí.
# ponytail: tope conocido — una página que vigile mutaciones de `style` vería la escritura. Si eso
# importa, el arreglo es `adoptedStyleSheets` por root, no volver a calcular cajas.

HIDE = ("color:transparent!important;text-shadow:none!important;background:#000!important;"
        "caret-color:transparent!important")


def tag_js(indices: list[int]) -> str:
    """JS que tapa los nodos secretos de `__MANOS_NODES` y devuelve cuántos tapó.

    El estilo original se guarda en el propio nodo y solo si no estaba guardado ya: con el array global
    de antes, dos capturas solapadas dejaban el HIDE soldado al atributo `style`. Idempotente significa
    que tapar dos veces y destapar una vez deja la página como estaba.
    """
    return """(() => {
  const N = globalThis.__MANOS_NODES || [];
  let n = 0;
  for (const i of %s) {
    const el = N[i];
    if (!el) continue;
    if (el.dataset.manosStyle === undefined) el.dataset.manosStyle = el.getAttribute('style') ?? '';
    el.setAttribute('data-manos-secret', '');
    el.setAttribute('style', el.dataset.manosStyle + ';%s');
    n++;
  }
  return n;
})()""" % (json.dumps(indices), HIDE)


# Destapa por consulta al DOM, no por un array global: así da igual quién tapó, cuántas veces, y si el
# mundo aislado murió entre medias. Barre también los shadow roots abiertos, donde `querySelectorAll`
# del documento no entra y donde vive el PIN del fixture.
UNTAG_JS = """(() => {
  const found = [];
  (function sweep(root) {
    for (const el of root.querySelectorAll('[data-manos-secret]')) found.push(el);
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) sweep(el.shadowRoot);
  })(document);
  for (const el of found) {
    const style = el.dataset.manosStyle;
    el.removeAttribute('data-manos-secret');
    delete el.dataset.manosStyle;
    if (style) el.setAttribute('style', style); else el.removeAttribute('style');
  }
  return found.length;
})()"""
