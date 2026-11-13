"""Autocomprobación de browser/sanitize.py:  uv run --project . tests/test_sanitize.py

Tres fixtures y tres propiedades distintas:

- `secrets.html` reproduce la tabla de fugas de PLAN §7.1 con valores reales en campos reales y la deja
  como regresión de `scrub()`: si una versión futura de Chrome deja de filtrar, o si alguien vuelve a
  meter `pierce:true` en el camino caliente, esto lo dice.
- `ataque.html` es el fixture adversario, y comprueba **la propiedad invertida**, que es la que de
  verdad importa: ningún valor de ningún campo de texto libre aparece en la salida que ve el modelo,
  lo haya reconocido el clasificador o no. De sus ocho campos el clasificador solo reconoce tres, y la
  fuga tiene que estar cerrada igual.
- `research/p1/ataque2.html` mide **el techo**: cuántas rutas llevan todavía un secreto al modelo y por
  dónde (`TECHO`), y a la vez que el listado **no** tacha lo que el agente escribió en un campo sin
  clasificar. Ese test falla si el número sube y también si baja sin actualizar la tabla.

Y la redacción de capturas, que se prueba tapando dos veces: el guardado tiene que ser idempotente o
dos capturas solapadas dejan el estilo de ocultación soldado a la página.
"""

import asyncio
import atexit
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser import observe, sanitize
from browser.session import BrowserSession


def tmpdir() -> Path:
    """Un directorio temporal que se borra al salir: dos `mkdtemp` sin limpiar por corrida dejaban
    decenas de directorios en /tmp."""
    path = Path(tempfile.mkdtemp(prefix="manos-captura-"))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "secrets.html").as_uri()
ATAQUE = (FIXTURES / "ataque.html").as_uri()
# El fixture de REVIEW-2 se queda donde lo dejó la revisión: es la medida de un techo, no un test que
# deba pasar, y `research/p1/probe_mcp.py` mide lo mismo por el cable MCP contra este mismo fichero.
ATAQUE2 = (Path(__file__).resolve().parent.parent / "research/p1/ataque2.html").as_uri()

# Literales de prueba. Ninguno es de nadie.
PASSWORD = "Tr0ub4dor&3"
OTP = "482913"
CARD = "4111111111111111"
PIN = "9182"
VALUES = {"#pw": PASSWORD, "#otp": OTP, "#card": CARD}

# Los del fixture adversario, por el nombre accesible con el que salen en el listado.
OTP_ES = "839214"
CARD_RAW = "5555444433332222"
CARD_VISTA = "5555 4444 3333 2222"   # lo que deja la máscara en el campo visible
PW_ABIERTO = "Rh1n0ceros!2026"
PW_CERRADO = "S0bre-cerrado!2026"
CE_OTP = "771903"
SEED = ("abandon ability able about above absent absorb abstract absurd abuse access accident "
        "account accuse achieve acid acoustic acquire across act action actor")
MAIL = "diego.pruebas@example.org"
BUSCA = "zapatos de piel"
CIUDAD = "Murcia"
assert len(SEED) > 120, "el secreto largo tiene que pasar de los 120 del truncado viejo"

# nombre accesible -> valor que acaba en el campo
CAMPOS = {
    "Código de un solo uso": OTP_ES,
    "Tarjeta": CARD_VISTA,
    "Contraseña de la caja": PW_ABIERTO,
    "Introduce el código que te hemos enviado": CE_OTP,
    "Frase de recuperación": SEED,
    "tu correo secreto": MAIL,
    "Buscar en secret stuff": BUSCA,
    "Ciudad": CIUDAD,
}

# Cuenta lo que una redacción mal destapada deja detrás: nodos marcados, estilos guardados sin
# restaurar, y estilos de ocultación soldados al atributo `style`. Cruza shadow roots abiertos.
RESTOS_JS = """(() => {
  let marked = 0, saved = 0, welded = 0;
  (function sweep(root) {
    for (const el of root.querySelectorAll('*')) {
      if (el.hasAttribute('data-manos-secret')) marked++;
      if (el.dataset.manosStyle !== undefined) saved++;
      if ((el.getAttribute('style') || '').includes('color:transparent')) welded++;
      if (el.shadowRoot) sweep(el.shadowRoot);
    }
  })(document);
  return {marked, saved, welded};
})()"""


# --- lo que no necesita navegador -------------------------------------------------------------------

assert sanitize.classify({"k": "password"}) == "A"
assert sanitize.classify({"k": "text", "ac": "one-time-code"}) == "A"
assert sanitize.classify({"k": "text", "ac": "cc-number"}) == "A"
assert sanitize.classify({"k": "text", "ac": "section-pago cc-csc"}) == "A", "autocomplete lleva tokens"
assert sanitize.classify({"k": "text", "n": "Verification code"}) == "B", "el OTP sin atributos duros"
assert sanitize.classify({"k": "text", "id": "cardNumber"}) == "B"
assert sanitize.classify({"k": "text", "nm": "iban"}) == "B"
assert sanitize.classify({"k": "text", "n": "Ciudad", "nm": "ciudad"}) == ""
# El nivel B marca los señuelos, y por eso tacha y escala en vez de bloquear el tecleo.
assert sanitize.classify({"k": "email", "ph": "tu correo secreto"}) == "B"
assert sanitize.classify({"k": "search", "ph": "Buscar en secret stuff"}) == "B"
# Y lo que el clasificador NO ve, que es la razón de que ya no sea la puerta.
assert sanitize.classify({"k": "text", "n": "Código de un solo uso", "id": "f1"}) == ""
assert sanitize.classify({"k": "div", "n": "Introduce el código que te hemos enviado"}) == ""
assert sanitize.classify({"k": "textarea", "n": "Frase de recuperación"}) == ""

# El estrangulamiento sobre estructuras anidadas, que es como llegan las respuestas CDP.
assert sanitize.scrub({"a": [{"nodeValue": PASSWORD}]}, {PASSWORD}) == {"a": [{"nodeValue": sanitize.REDACTED}]}
assert sanitize.scrub("x" + OTP + "y", {OTP}) == "x" + sanitize.REDACTED + "y"
assert sanitize.scrub("ab", {"ab"}) == "ab", "por debajo de MIN_LEN no se tacha"
# La máscara del AX filtra la longitud de la contraseña: también se tacha. Pero solo viñetas: con los
# asteriscos dentro, cualquier `****` de cualquier página se llevaba un «redactado».
assert sanitize.scrub("•" * 11, set()) == sanitize.REDACTED
assert sanitize.scrub("*" * 8, set()) == "*" * 8, "ocho asteriscos no son una máscara"
assert sanitize.scrub("**oferta** · · · ·", set()) == "**oferta** · · · ·"

# `clean_url`: fuera el valor de los parámetros con pinta de credencial, dentro el nombre.
assert sanitize.clean_url("https://x.es/cb?code=AUTH-1&next=/panel") == \
    f"https://x.es/cb?code={sanitize.REDACTED}&next=/panel"
assert sanitize.clean_url("https://x.es/r#access_token=T&id_token=U") == \
    f"https://x.es/r#access_token={sanitize.REDACTED}&id_token={sanitize.REDACTED}"
assert sanitize.clean_url("https://x.es/a?csrf_token=Z") == \
    f"https://x.es/a?csrf_token={sanitize.REDACTED}", "cualquier parámetro que acabe en _token"
assert sanitize.clean_url("https://x.es/b?q=zapatos&sort=1#seccion-2") == \
    "https://x.es/b?q=zapatos&sort=1#seccion-2", "una url sin credenciales no se toca"

# `classify_rows` ya no ve valores de texto libre: la cosecha le manda la longitud (`vl`).
rows = [{"k": "password", "vl": 11}, {"k": "text", "ac": "one-time-code", "vl": 6},
        {"k": "text", "nm": "ciudad", "vl": 6}]
sanitize.forget()
assert sanitize.classify_rows(rows) == 2
assert rows[0]["sec"] == "A" and rows[1]["sec"] == "A" and "sec" not in rows[2]
assert sanitize.known() == frozenset(), "sin valores que memorizar, no memoriza nada"
# Un valor estructural en una fila marcada sí se quita y se memoriza.
estructural = [{"k": "month", "ac": "cc-exp", "v": "2031-07"}]
assert sanitize.classify_rows(estructural) == 1 and "v" not in estructural[0]
assert sanitize.known() == {"2031-07"}
assert sanitize.scrub({"eco": f"caduca el {PASSWORD}"}, {PASSWORD})["eco"].endswith(sanitize.REDACTED)

# El conjunto vivo tiene tope: `scrub()` cuesta una pasada por valor en cada respuesta.
sanitize.forget()
sanitize.remember([f"valor-{i}" for i in range(sanitize.MAX_KNOWN + 10)])
assert len(sanitize.known()) == sanitize.MAX_KNOWN
assert "valor-0" not in sanitize.known(), "se desaloja lo más viejo"
assert f"valor-{sanitize.MAX_KNOWN + 9}" in sanitize.known()
sanitize.forget()
assert sanitize.known() == frozenset()
print("sanitize: clasificación, tope del conjunto y estrangulamiento  OK")


# --- contra un navegador de verdad ------------------------------------------------------------------

async def fill(cdp, selector, value, host=None):
    """Teclea de verdad: el secreto tiene que llegar al shadow DOM interno del input como lo haría
    escribiéndolo, no por asignación de `.value`, que además no dispara la máscara de la tarjeta."""
    js = (f"document.querySelector('{host}').shadowRoot.querySelector('{selector}').focus()"
          if host else f"document.querySelector('{selector}').focus()")
    await cdp.send("Runtime.evaluate", {"expression": js})
    await cdp.send("Input.insertText", {"text": value})


async def restos(cdp) -> dict:
    got = await cdp.send("Runtime.evaluate", {"expression": RESTOS_JS, "returnByValue": True})
    return got["result"]["value"]


async def tabla_de_fugas(session, cdp):
    """PLAN §7.1 contra el navegador instalado, y `scrub()` sobre las respuestas crudas."""
    await session.page.goto(PAGE)
    for selector, value in VALUES.items():
        await fill(cdp, selector, value)
    await fill(cdp, "#pin", PIN, host="caja-fuerte")

    probes = {
        "DOM.getDocument{depth:-1}": ("DOM.getDocument", {"depth": -1}),
        "DOM.getDocument{pierce:true}": ("DOM.getDocument", {"depth": -1, "pierce": True}),
        "Accessibility.getFullAXTree": ("Accessibility.getFullAXTree", {}),
        "DOMSnapshot.captureSnapshot": ("DOMSnapshot.captureSnapshot", {"computedStyles": []}),
        "DOM.getOuterHTML": ("DOM.getOuterHTML", None),
    }
    table, raw = {}, {}
    for label, (method, params) in probes.items():
        if params is None:  # el nodeId caduca con cada getDocument, así que se relee aquí mismo
            params = {"nodeId": (await cdp.send("DOM.getDocument", {}))["root"]["nodeId"]}
        got = await cdp.send(method, params)
        raw[label] = got
        text = json.dumps(got, ensure_ascii=False)
        table[label] = {"contraseña": PASSWORD in text, "OTP": OTP in text, "tarjeta": CARD in text,
                        "máscara": bool(sanitize.MASKED.search(text))}

    print("\ntabla de fugas medida ahora mismo (True = el secreto sale en claro):")
    for label, cells in table.items():
        print(f"  {label:34s} {cells}")

    # Las cuatro celdas que deciden el diseño. Si alguna cambia, el diseño hay que repensarlo,
    # así que el test falla en vez de pasar en silencio.
    assert not any(table["DOM.getDocument{depth:-1}"][k] for k in ("contraseña", "OTP", "tarjeta")), \
        "getDocument sin pierce dejó de estar limpio"
    assert table["DOM.getDocument{pierce:true}"]["contraseña"], \
        "pierce:true dejó de filtrar la contraseña en claro: vuelve a medir antes de fiarte"
    assert table["Accessibility.getFullAXTree"]["OTP"], "el AX dejó de filtrar el OTP"
    assert table["Accessibility.getFullAXTree"]["máscara"], \
        "el AX dejó de enmascarar la contraseña en viñetas (la máscara filtra la longitud)"
    assert table["DOMSnapshot.captureSnapshot"]["contraseña"], "captureSnapshot dejó de filtrar"
    assert not table["DOM.getOuterHTML"]["contraseña"], "getOuterHTML dejó de estar limpio"

    # Y ahora lo que de verdad hay que garantizar: el estrangulamiento tapa las cinco.
    sanitize.remember(list(VALUES.values()) + [PIN])
    for label, got in raw.items():
        clean = json.dumps(sanitize.scrub(got), ensure_ascii=False)
        for name, value in (("contraseña", PASSWORD), ("OTP", OTP), ("tarjeta", CARD), ("PIN", PIN)):
            assert value not in clean, f"{label} siguió filtrando {name} después de scrub()"
        assert not sanitize.MASKED.search(clean), f"{label} siguió filtrando la longitud tras scrub()"
    print("scrub(): las 5 llamadas quedan limpias de los 4 secretos  OK")


async def listado_sin_valores(session):
    """El camino real sobre `secrets.html`: ni un valor, y los campos marcados."""
    sanitize.forget()
    observe.reset()
    result = await observe.observe(session)
    listing = "\n".join(result["listing"])
    for value in (PASSWORD, OTP, CARD, PIN):
        assert value not in listing, "observe() filtró un secreto"
    assert result["secrets"] >= 7, f"se esperaban >=7 campos marcados, salieron {result['secrets']}"
    assert "[secreto:A]" in listing and "[secreto:B]" in listing
    ciudad = [ln for ln in result["listing"] if "Ciudad" in ln]
    assert ciudad and "[secreto" not in ciudad[0], "el campo normal no se marca"
    print(f"observe(): {result['secrets']} campos marcados, 0 valores filtrados  OK")
    for line in result["listing"]:
        print("   ", line)
    return result


async def redaccion(session, cdp, out: Path):
    """Tapar, capturar, destapar — incluido el caso de dos capturas solapadas."""
    tapped = await observe.screenshot(session, str(out / "redact.jpg"))
    assert tapped["redacted_fields"] >= 7, tapped
    assert await restos(cdp) == {"marked": 0, "saved": 0, "welded": 0}, "la redacción no se destapó"

    fid = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
    idx = observe._secrets[fid]

    # Tapar dos veces y destapar una: es lo que le pasa a la página con dos capturas solapadas. Con el
    # array global de antes, el segundo tapado guardaba como «original» el estilo que ya llevaba el
    # HIDE y la página se quedaba con seis inputs en negro para siempre.
    await observe._eval(cdp, fid, sanitize.tag_js(idx))
    await observe._eval(cdp, fid, sanitize.tag_js(idx))
    assert await observe._eval(cdp, fid, sanitize.UNTAG_JS) == len(idx)
    assert await restos(cdp) == {"marked": 0, "saved": 0, "welded": 0}, \
        "tapar dos veces y destapar una dejó restos: el guardado no es idempotente"
    print(f"redacción: tapar x2 y destapar x1 deja la página limpia ({len(idx)} nodos)  OK")

    # Y de verdad, en paralelo. Lo que se asierta es que la página queda limpia; que las dos capturas
    # salgan tapadas lo garantiza el candado de `session.exclusive()`, no esto.
    a, b = await asyncio.gather(observe.screenshot(session, str(out / "a.jpg")),
                                observe.screenshot(session, str(out / "b.jpg")))
    assert a["redacted_fields"] == b["redacted_fields"] == len(idx), (a, b)
    assert await restos(cdp) == {"marked": 0, "saved": 0, "welded": 0}, \
        "dos capturas solapadas dejaron la página tachada"
    print(f"redacción: 2 capturas en paralelo, {a['redacted_fields']} campos cada una, 0 restos  OK")

    # La captura sin nada que tapar, para comparar bytes.
    sanitize.forget()
    observe.reset()
    await observe.observe(session)
    observe._secrets.clear()  # misma página, sin tapar nada
    plain = await observe.screenshot(session, str(out / "plain.jpg"))
    assert plain["redacted_fields"] == 0
    assert tapped["bytes"] != plain["bytes"], "la captura tapada salió idéntica a la limpia"
    print(f"captura: {tapped['redacted_fields']} campos tapados, "
          f"{tapped['bytes']} B tapada contra {plain['bytes']} B limpia  OK")

    # F5: el mundo aislado se recreó entre la cosecha y la captura, así que `__MANOS_NODES` vuelve
    # vacío y no se tapa nada. Antes esto daba una imagen con los secretos a la vista y
    # `redacted_fields: 0` que nadie comprobaba.
    await observe.observe(session)
    await observe._eval(cdp, fid, "globalThis.__MANOS_NODES = []; 0")
    try:
        await observe.screenshot(session, str(out / "nunca.jpg"))
        raise AssertionError("la captura no se quejó de no haber tapado ni un campo")
    except RuntimeError as exc:
        assert "browser_observe" in str(exc), exc
    assert await restos(cdp) == {"marked": 0, "saved": 0, "welded": 0}
    print("captura: 0 campos tapados habiendo secretos esperados → error ruidoso  OK")


async def propiedad_invertida(session, cdp):
    """`ataque.html`: ningún valor de ningún campo de texto libre sale, lo reconozca o no el clasificador."""
    await session.page.goto(ATAQUE)
    sanitize.forget()
    observe.reset()
    for selector, value in (("#f1", OTP_ES), ("#f2", CARD_RAW), ("#f3", CE_OTP), ("#f4", SEED),
                            ("#f5", MAIL), ("#f6", BUSCA), ("#ciudad", CIUDAD)):
        await fill(cdp, selector, value)
    await fill(cdp, "#pw-abierto", PW_ABIERTO, host="caja-abierta")
    # El root cerrado no se puede consultar desde fuera: se teclea por el gancho de la propia página.
    await cdp.send("Runtime.evaluate", {"expression": "enfocaCerrado()"})
    await cdp.send("Input.insertText", {"text": PW_CERRADO})

    result = await observe.observe(session)
    blob = json.dumps(result, ensure_ascii=False)
    print("\nlistado del fixture adversario tal cual lo ve el modelo:")
    for line in result["listing"]:
        print("   ", line)

    # LA propiedad: ni un valor, de ningún campo, ni el crudo del hidden ni el formateado por la máscara.
    for nombre, valor in CAMPOS.items():
        assert valor not in blob, f"el valor de «{nombre}» salió al modelo"
    assert CARD_RAW not in blob, "los dígitos crudos del input hidden salieron al modelo"
    assert PW_CERRADO not in blob

    # Y la longitud sí sale, que es lo que el modelo necesita: prueba de que el campo se cosechó.
    for nombre, valor in CAMPOS.items():
        lineas = [ln for ln in result["listing"] if f'"{nombre}"' in ln]
        assert len(lineas) == 1, f"se esperaba una línea para «{nombre}», salieron {lineas}"
        assert f"len={len(valor)}" in lineas[0], lineas[0]
    assert result["elements"] == 8, f"se esperaban 8 filas, salieron {result['elements']}"
    assert not any("card_raw" in ln for ln in result["listing"]), \
        "el input hidden de la máscara no debe emitir fila: su valor solo se aprende"

    # El clasificador sigue reconociendo 3 de 8 —la contraseña y los dos señuelos— y ya no importa.
    assert result["secrets"] == 3, f"el fixture reconoce 3 campos; salieron {result['secrets']}"
    for senuelo in ("tu correo secreto", "Buscar en secret stuff"):
        linea = [ln for ln in result["listing"] if f'"{senuelo}"' in ln][0]
        assert "[secreto:B]" in linea, linea
    # Los dos señuelos se teclearon igual (sus `len=` cuadran arriba): el nivel B no bloquea.

    # Lo aprendido: el valor de los campos MARCADOS y el del `type=hidden`, que no emite fila. Y nada
    # más: aprender el de un campo visible sin clasificar tacha después la diana que el agente buscaba.
    aprendido = sanitize.known()
    for valor in (CARD_RAW, PW_ABIERTO, MAIL, BUSCA):
        assert valor in aprendido, f"no se aprendió {valor[:16]!r}: scrub() queda ciego a él"
    for valor in (OTP_ES, CARD_VISTA, CE_OTP, SEED, CIUDAD):
        assert valor not in aprendido, f"se aprendió {valor[:16]!r}, de un campo sin clasificar"
    eco = sanitize.scrub({"crudo": " | ".join(aprendido)})["crudo"]
    for valor in (CARD_RAW, PW_ABIERTO, MAIL):
        assert valor not in eco, "lo aprendido tiene que tacharse donde reaparezca"

    # El techo, dicho en una aserción: un shadow root CERRADO no se cosecha, así que su valor no se
    # aprende y `scrub()` no puede tacharlo. La defensa es no emitir valores, no clasificar mejor.
    assert PW_CERRADO not in aprendido, "la cosecha ya cruza un root cerrado: revisa este techo"
    print(f"ataque.html: {result['elements']} campos, {result['secrets']} reconocidos, "
          f"{len(aprendido)} valores aprendidos, 0 valores emitidos  OK")

    # La captura tiene que tapar TODO campo de texto libre, no solo los 3 clasificados: una imagen es
    # contexto para el modelo igual que el texto, y taparla solo por `sec` dejaba 5 secretos legibles
    # en los píxeles, que es la misma fuga por otra ruta.
    libres = sum(1 for ln in result["listing"] if "len=" in ln)
    tapados = await observe.screenshot(
        session, str(tmpdir() / "ataque.jpg"))
    assert tapados["redacted_fields"] == libres, (
        f"se taparon {tapados['redacted_fields']} de {libres} campos de texto libre")
    assert tapados["redacted_fields"] > result["secrets"], (
        "la captura solo tapó los clasificados: los no clasificados salen legibles")
    print(f"captura de ataque.html: {tapados['redacted_fields']} de {libres} campos de texto libre "
          f"tapados ({result['secrets']} clasificados)  OK")

    # Con `viewport_only=True` un campo bajo el pliegue no emite fila, así que tampoco se clasifica y
    # su valor no se aprende. Es el techo del estrechamiento, y está escrito en `sanitize`.
    await cdp.send("Emulation.setDeviceMetricsOverride",
                   {"width": 1280, "height": 120, "deviceScaleFactor": 1, "mobile": False})
    sanitize.forget()
    observe.reset()
    corto = await observe.observe(session)
    assert corto["skipped_offviewport"] >= 1, corto
    assert not any("Frase de recuperación" in ln for ln in corto["listing"]), \
        "el fixture cambió de alto: la semilla ya no queda bajo el pliegue"
    assert SEED not in sanitize.known(), "un campo sin clasificar bajo el pliegue no se aprende"
    assert SEED not in json.dumps(corto, ensure_ascii=False), "y aun así su valor no sale"
    print(f"viewport corto: {corto['elements']} filas, {corto['skipped_offviewport']} descartadas "
          f"por el pliegue, y el valor de abajo sin emitir y sin aprender  OK")


# El techo, medido. Cada secreto de `ataque2.html` con su ruta y si HOY cruza al modelo. El valor de
# esta tabla no es que pase: es que el día que alguien cierre una ruta tenga que quitarla de aquí, y que
# una ruta nueva no pueda abrirse en silencio. Mismo argumento que `renderer_seccomp`.
TECHO = {
    # cerradas: el valor de un campo no sale, ni crudo ni como lo deja la máscara
    "839214":                    ("R1 OTP type=text, aria-label en español", False),
    "5555444433332222":          ("R2 tarjeta, dígitos crudos en input type=hidden", False),
    "5555 4444 3333 2222":       ("R2 tarjeta, lo que deja la máscara a la vista", False),
    "Rh1n0ceros!2026":           ("R3 contraseña en shadow root ABIERTO", False),
    "771903":                    ("R5 código de un solo uso en contenteditable", False),
    SEED:                        ("R6 frase semilla de 153 caracteres", False),
    "diego.pruebas@example.org": ("R7 señuelo: email con «secreto» en el placeholder", False),
    "zapatos de piel":           ("R7 señuelo: búsqueda con «secret» en el placeholder", False),
    "OAUTH-CODE-77219":          ("N8 ?code= de OAuth en la url, recortado por clean_url", False),
    "RESET-TK-4412":             ("N8 token de restablecimiento en la url, recortado", False),
    "IMG-VAL-8821-QQ":           ("N12 value de un input type=image (el GIF de 1x1 ni emite fila)", False),
    # abiertas: texto que la página escribe fuera de un campo. `scrub()` no sabe que es un secreto, y
    # taparlo dejaría la página ilegible para el agente. Es el techo, no un defecto que quede por tapar.
    "S0bre-cerrado!2026":        ("R4 shadow root CERRADO, espejado al texto de un botón", True),
    "ZXQA-9931-KTPV":            ("N1 nombre accesible (aria-label de un botón)", True),
    "tok_live_8H3K9QW2":         ("N2 atributo title", True),
    "4471-XRT":                  ("N3 placeholder usado como nombre", True),
    "4916-2211-9087-3344":       ("N4 texto de una <option>", True),
    "ES7620770024003102575766":  ("N4 valor de una <option>", True),
    "BKUP-7721-9034":            ("N5a texto visible de un enlace", True),
    "ERR-5540-QQ":               ("N6 aria-describedby", True),
    "CVAL-3398-ZM":              ("N7 validationMessage (setCustomValidity)", True),
    "1120-9931":                 ("N12 value de un submit, por el nombre accesible", True),
    "RST-2204-LK":               ("N12 value de un reset, por el nombre accesible", True),
    "PAT-4471":                  ("N13 atributo pattern", True),
    "TTL-9931-AB":               ("N9 título de la página", True),
    "DLG-6677-PK":               ("N10 texto de un diálogo, por el mensaje de DialogOpen", True),
}

URL2 = (f"{ATAQUE2}?fill=1&code=OAUTH-CODE-77219&token=RESET-TK-4412"
        f"&seed={SEED.replace(' ', '%20')}")


async def techo_medido(session, cdp):
    """`ataque2.html`: cuántas rutas llevan todavía un secreto al modelo, y la otra cara —que nada de
    lo que el agente escribió en un campo sin clasificar se tache en el listado siguiente."""
    await cdp.send("Emulation.setDeviceMetricsOverride",
                   {"width": 1280, "height": 2400, "deviceScaleFactor": 1, "mobile": False})
    sanitize.forget()
    observe.reset()
    await session.page.goto(URL2)
    result = await observe.observe(session, viewport_only=False)
    blob = json.dumps(result, ensure_ascii=False)
    listing = "\n".join(result["listing"])
    print("\nlistado de ataque2.html tal cual lo ve el modelo:")
    for line in result["listing"]:
        print("   ", line)

    # El texto de un diálogo llega por el mensaje de DialogOpen, que es otra ruta y se mide igual.
    await session.page.goto(URL2 + "&dialog=1")
    await asyncio.sleep(0.5)
    try:
        await observe.observe(session)
        dialogo = ""
    except Exception as exc:  # DialogOpen
        dialogo = json.dumps(sanitize.scrub({"error": str(exc)}), ensure_ascii=False)
    assert dialogo, "el alert del fixture no se abrió: la ruta del diálogo no se midió"
    await session.answer_dialog(True)
    blob += dialogo

    fugas = {t for t in TECHO if t in blob}
    esperadas = {t for t, (_ruta, sale) in TECHO.items() if sale}
    print(f"\ntecho medido: {len(fugas)} de {len(TECHO)} secretos cruzan al modelo")
    for token, (ruta, _sale) in TECHO.items():
        if token in fugas:
            print(f"    FUGA  {ruta}")
    assert not fugas - esperadas, \
        f"el techo SUBIÓ, rutas nuevas: {[TECHO[t][0] for t in fugas - esperadas]}"
    assert not esperadas - fugas, ("el techo bajó, que es la buena noticia: quita de TECHO las rutas "
                                   f"ya cerradas {[TECHO[t][0] for t in esperadas - fugas]}")

    # La otra cara del mismo mecanismo: lo que el agente teclea en un campo sin clasificar NO se
    # aprende, así que la diana legítima cuyo nombre lo repite sigue siendo legible y pulsable.
    assert 'link "Enviar a Murcia"' in listing, "sobre-saneado: la diana legítima salió tachada"
    assert '"Ciudad"  len=6' in listing, "el campo sin clasificar dejó de emitir su longitud"
    # Y el precio del nivel B, deliberado: el buscador señuelo lleva «secret» en el placeholder, así
    # que su valor sí se aprende y su eco sí se tacha. Bloquear el tecleo sería peor.
    assert 'link "Más «redactado»"' in listing, listing
    # El `value` de un submit o un reset ya no se imprime: era el nombre accesible por segunda vez.
    assert 'button "Confirmar 1120-9931"' in listing and '= "Confirmar 1120-9931"' not in listing
    # Un campo de texto vacío dice `len=0` en vez de callarse, que no se distinguía de «sin valor».
    assert '"Serie"  len=0' in listing, listing
    print("sobre-redacción: «Enviar a Murcia» entero, «Ciudad» con len=6, y el eco del señuelo B "
          "tachado  OK")


async def main():
    session = await BrowserSession.start()
    try:
        cdp = await session.cdp()
        await cdp.send("DOM.enable")
        await cdp.send("Accessibility.enable")
        # Viewport fijo y alto: nada de lo que mide este test puede depender de lo que KWin le dé a la
        # ventana, y nada puede achacarse a estar bajo el pliegue.
        await cdp.send("Emulation.setDeviceMetricsOverride",
                       {"width": 1280, "height": 2000, "deviceScaleFactor": 1, "mobile": False})
        await tabla_de_fugas(session, cdp)
        await listado_sin_valores(session)
        await redaccion(session, cdp, tmpdir())
        await propiedad_invertida(session, cdp)
        await techo_medido(session, cdp)
    finally:
        await session.close()
        sanitize.forget()
        observe.reset()


asyncio.run(main())
print("\ntest_sanitize: OK")
