"""Autocomprobación de browser/forms.py:  uv run --project . tests/test_forms.py

Mide el subsistema de formularios contra fixtures propios, nunca contra un sitio real: aquí se envía un
formulario de verdad, y eso solo se hace contra un `http.server` de la biblioteca estándar.

Lo que comprueba, en el orden en que corre:

 1. el cargador del perfil: composición, derivación, y que una clave desconocida o con pinta de secreto
    es un error de carga ruidoso y no un campo silenciosamente vacío;
 2. la whitelist del Autofill, contra `research/p2/sweep-results.json`, que es la medida: 54 nombres, y
    los 10 que matan el proceso del navegador fuera;
 3. **una sola regla del valor**: la cosecha de `forms` y la de `observe` dicen la misma longitud campo
    por campo, porque dos reglas en un repositorio son una fuga esperando turno;
 4. los 24 campos del formulario español, rellenos y verificados de punta a punta, con la latencia real
    por campo;
 5. la regla del secreto, asertada POR VALOR sobre el JSON entero de las tres tools;
 6. la entrada controlada: el espejo del componente cambia, que es la prueba de que no se asignó
    `.value`;
 7. la puerta previa al envío pillando un campo obligatorio en un paso oculto, un token por cada botón
    de envío visible, el envío de verdad con su evidencia y el freno del doble envío;
 8. **el techo de la relectura**: la tabla de rutas por las que un valor que el servidor resolvió desde
    `{{perfil.clave}}` podría volverle al modelo, asertada a CERO sobre un fixture en el que el
    clasificador no marca nada, porque la protección no puede depender de que acierte;
 9. una sola clasificación por campo: `browser_observe` y `form_describe` tienen que decir el mismo
    nivel del mismo campo, que es lo que falla cuando hay dos cadenas de etiquetado;
10. el traspaso de pago por el camino positivo, con dos orígenes locales;
11. el Autofill: una llamada, el diff de lo que pisó, y una caída inducida de la que se vuelve
    cerrando y reabriendo — que es la única ruta que funciona.
"""

import asyncio
import atexit
import functools
import http.server
import json
import os
import re
import resource
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser import forms, observe, sanitize
from browser.session import PROFILE as BROWSER_PROFILE, BrowserSession

FIXTURES = Path(__file__).parent / "fixtures"
RESEARCH = Path(__file__).resolve().parent.parent / "research/p2/sweep-results.json"

# Los valores que el fixture lleva puestos en los campos de nivel A. Son la diana del test de la regla
# del secreto: ninguna de las tres tools puede dejar que ninguno de los tres salga, por ninguna ruta.
CLAVE = "Tr3nd3Murcia!"
TARJETA = "4111111111111111"
OTP = "889901"

PROFILE = """
nombre    = "Lucía"
apellido1 = "Ramírez"
apellido2 = "Beltrán"
dni = "12345678Z"
nacimiento = "1990-12-25"
telefono = "+34600112233"
email    = "lucia.ramirez@example.es"
iban = "ES9121000418450200051332"
cv = "{cv}"

[direccion]
via       = "Calle Mayor"
numero    = "14"
piso      = "3"
puerta    = "B"
cp        = "30001"
municipio = "Murcia"
provincia = "Murcia"
pais      = "ES"
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    """Sirve los fixtures y contesta el POST del formulario con un 303 a la página de aterrizaje, que es
    la forma normal de un envío correcto y la que deja medir la cadena de evidencias."""

    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(303)
        self.send_header("Location", "/form-ok.html")
        self.end_headers()


def serve() -> int:
    handler = functools.partial(Handler, directory=str(FIXTURES))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def deep(obj):
    """Cada cadena del JSON, a cualquier profundidad. Es como se asierta «por valor»: no «la clave
    `value` no está», sino «esa cadena no aparece en ninguna parte de la respuesta»."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from deep(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from deep(v)


def no_aparece(blob, *secretos):
    text = json.dumps(blob, ensure_ascii=False)
    for s in secretos:
        assert s not in text, f"FUGA: «{s}» salió en la respuesta"
        for piece in deep(blob):
            assert s not in piece, f"FUGA: «{s}» salió dentro de {piece[:80]!r}"


def row(result, label):
    got = [r for r in result["readback"] if r["label"] == label]
    assert len(got) == 1, f"se esperaba una fila para «{label}», hay {len(got)}"
    return got[0]


def fid(described, label):
    got = [f["i"] for f in described["fields"] if f["label"] == label]
    assert len(got) == 1, f"se esperaba un campo «{label}», hay {len(got)}: " \
                          f"{[f['label'] for f in described['fields']]}"
    return got[0]


# --- 1. el cargador del perfil ----------------------------------------------------------------------

def test_profile() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="manos-perfil-"))
    # El directorio vive hasta el final —el `cv.pdf` de dentro se sube en la tanda de 25 campos— así
    # que se borra al salir y no en un `finally`. Sin esto cada corrida deja un perfil en /tmp.
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    cv = tmp / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 no es un pdf de verdad, pero existe y pesa\n")
    good = tmp / "profile.toml"
    good.write_text(PROFILE.format(cv=cv))
    os.chmod(good, 0o600)

    loaded = forms.load_profile(good)
    assert not loaded["warning"], loaded["warning"]
    assert "nombre" in loaded["keys"] and "direccion.cp" in loaded["keys"]
    assert all(k in forms.PROFILE_KEYS for k in loaded["keys"])
    # Las CLAVES salen, los valores no: es lo que hace que el modelo planifique con claves.
    no_aparece(loaded, "Lucía", "ES9121000418450200051332")

    # Composición: un solo campo «Nombre y apellidos» se rellena uniendo tres claves.
    assert forms.resolve("{{perfil.nombre}} {{perfil.apellido1}} {{perfil.apellido2}}") \
        == "Lucía Ramírez Beltrán"
    assert forms.resolve("{{perfil.direccion.cp}}") == "30001"
    assert forms.resolve("sin referencias") == "sin referencias"
    assert forms.resolve(True) is True

    # Una clave que no existe es un error ruidoso con la línea y la clave parecida, nunca un vacío.
    for bad, trozo in ((f'telefono_movil = "+34600112233"\n', "¿Querías decir «telefono»?"),
                       ('tarjeta = "4111111111111111"\n', "tiene pinta de secreto"),
                       ('contrasena = "x"\n', "tiene pinta de secreto"),
                       ('nombre = 42\n', "tiene que ser una cadena"),
                       ('nacimiento = "25/12/1990"\n', "va en ISO")):
        path = tmp / "bad.toml"
        path.write_text(bad)
        try:
            forms.load_profile(path)
        except forms.ProfileError as exc:
            assert trozo in str(exc), f"{bad.strip()} -> {exc}"
            assert "No se ha cargado ningún valor" in str(exc)
        else:
            raise AssertionError(f"{bad.strip()} tenía que ser un error de carga")
    # Y el rechazo es total: tras un fichero malo no queda medio perfil en memoria.
    assert forms.resolve.__module__ and not forms._profile

    path = tmp / "dni.toml"
    path.write_text('dni = "12345678A"\n')
    try:
        forms.load_profile(path)
    except forms.ProfileError as exc:
        assert "letra de control" in str(exc)
    else:
        raise AssertionError("una letra de DNI equivocada tenía que ser un error de carga")

    # Un fichero legible por otros carga, pero avisa una vez.
    os.chmod(good, 0o644)
    assert "legible por otros usuarios" in forms.load_profile(good)["warning"]
    os.chmod(good, 0o600)
    forms.load_profile(good)
    print(f"perfil: {len(loaded['keys'])} claves, composición y derivación OK, "
          f"6 ficheros malos rechazados con su línea  OK")
    return str(cv)


# --- 2. la whitelist del Autofill -------------------------------------------------------------------

def test_whitelist() -> None:
    sweep = json.loads(RESEARCH.read_text())
    ok = {r["name"] for r in sweep if r["verdict"] == "ok"}
    crash = {r["name"] for r in sweep if r["verdict"] == "CRASH"}
    assert len(ok) == 54 and len(crash) == 10, (len(ok), len(crash))
    assert forms.AUTOFILL_SAFE == ok, ("la whitelist se ha desviado de la medida: "
                                       f"faltan {sorted(ok - forms.AUTOFILL_SAFE)}, "
                                       f"sobran {sorted(forms.AUTOFILL_SAFE - ok)}")
    assert not (forms.AUTOFILL_SAFE & crash)
    assert "PHONE_HOME_WHOLE_NUMBER" in forms.AUTOFILL_SAFE
    assert not any(n.startswith("PHONE_HOME_") and n != "PHONE_HOME_WHOLE_NUMBER"
                   for n in forms.AUTOFILL_SAFE)
    # Y un nombre fuera de la lista no se pasa: se rechaza aquí, porque uno de ellos mata el proceso.
    try:
        forms.autofill_address({"nombre": "Lucía", "telefono": "+34600112233"} |
                               {"direccion.pais": "ES"})
    except forms.FormError:
        raise AssertionError("un perfil legítimo no puede ser rechazado")
    assert all(f["name"] in forms.AUTOFILL_SAFE for f in forms.autofill_address())
    # Y la whitelist se prueba por donde se aplica: metiendo cada nombre mortal en el mapa y
    # comprobando que `autofill_address` se niega. Uno de los diez mata el proceso del navegador y de
    # esa caída no se vuelve, así que el rechazo no puede depender de que el mapa sea una constante.
    guardado = forms.AUTOFILL_MAP
    try:
        for mortal in sorted(crash) + ["NAME_FIRST "]:          # con el espacio final incluido
            forms.AUTOFILL_MAP = (("nombre", mortal),)
            try:
                forms.autofill_address({"nombre": "Lucía"})
            except forms.FormError:
                continue
            raise AssertionError(f"«{mortal}» pasó la whitelist")
    finally:
        forms.AUTOFILL_MAP = guardado
    print(f"whitelist del Autofill: {len(forms.AUTOFILL_SAFE)} nombres, los 10 mortales fuera y "
          f"rechazados uno a uno por autofill_address()  OK")


# --- el cuerpo, con navegador -----------------------------------------------------------------------

# --- 6-bis. el techo de la relectura: un valor del perfil no vuelve por ninguna ruta ----------------
# Con la forma del TECHO de test_sanitize.py: la tabla de rutas por las que un valor que resolvió el
# SERVIDOR desde `{{perfil.clave}}` podría volverle al modelo, asertada a CERO, y que falla si sube.
#
# Dos cosas que el fixture hace a propósito, porque sin ellas el test pasa con el agujero abierto:
#  · NINGÚN campo lo marca el clasificador. La protección no puede depender de que acierte: ahí vivía
#    la fuga, y el IBAN y el DNI cruzaron el cable MCP en claro por exactamente esos dos campos.
#  · cada valor va a UN solo campo, en su propia tanda. La fuga era dependiente del orden: cuando el
#    mismo valor iba además a un campo marcado, se memorizaba y salía tachado de carambola, así que un
#    test que meta los dos campos juntos pasa y el agujero sigue ahí.
PERFIL_VALORES = {
    "ES9121000418450200051332": "{{perfil.iban}} en «Número de cuenta bancaria» (sin marcar)",
    "12345678Z": "{{perfil.dni}} en «Documento de identidad» (sin marcar)",
    "1990-12-25": "{{perfil.nacimiento}} en un type=date",
    "25/12/1990": "la misma fecha como la deriva el servidor (de un campo estructural sí sale valor)",
}


async def techo_del_perfil(base: str) -> None:
    session = await BrowserSession.start(f"{base}/form-perfil.html")
    rutas: dict[str, object] = {}
    try:
        d = await forms.describe(session)
        assert not [f for f in d["fields"] if f.get("sec")], \
            "este fixture no vale si el clasificador marca algún campo: el punto es que falle"
        ids = {f["label"]: f["i"] for f in d["fields"]}

        # (a) dry_run, que no toca la página: el `check` que falla CITA el valor, ya derivado.
        seco = await forms.fill(session, [{"id": ids["Referencia"], "value": "{{perfil.dni}}"}],
                                dry_run=True)
        ref = [p for p in seco["plan"] if p["label"] == "Referencia"][0]
        assert "no cumple pattern" in ref["check"], ref
        assert sanitize.REDACTED in ref["check"], f"el check citó el DNI en claro: {ref['check']}"
        assert ref["sent"] == "{{perfil.dni}}", ref
        assert await session.page.evaluate("document.getElementById('ref').value") == "", \
            "el dry_run escribió en la página"
        rutas["comprobación de formato de dry_run"] = seco

        # (b) la relectura, y con ella el needs_value del obligatorio y el validationMessage que la
        #     propia página se escribe con el valor dentro.
        relleno = await forms.fill(session, [
            {"id": ids["Número de cuenta bancaria"], "value": "{{perfil.iban}}"},
            {"id": ids["Documento de identidad"], "value": "{{perfil.dni}}"},
            {"id": ids["Fecha de nacimiento"], "value": "{{perfil.nacimiento}}"}])
        # «Documento de identidad» sale `needs_different_value` a propósito: la página lo rechaza con
        # un mensaje propio, que es la ruta que se está midiendo. Lo que importa es que el valor entró
        # (la longitud) y que el mensaje vuelve tachado.
        for label, clave, largo, veredicto in (
                ("Número de cuenta bancaria", "{{perfil.iban}}", 24, "ok"),
                ("Documento de identidad", "{{perfil.dni}}", 9, "needs_different_value"),
                ("Fecha de nacimiento", "{{perfil.nacimiento}}", 10, "ok")):
            r = row(relleno, label)
            assert r["verdict"] == veredicto, r
            assert "value" not in r, f"la relectura de «{label}» enseñó un valor del perfil: {r}"
            assert r["sent"] == clave and r["len"] == largo, r
        assert [n["label"] for n in relleno["needs_value"]] == ["Razón"], relleno["needs_value"]
        doc = row(relleno, "Documento de identidad")
        assert doc.get("valid") == 0 and sanitize.REDACTED in doc.get("msg", ""), \
            f"el validationMessage de la página echó el DNI de vuelta: {doc}"
        rutas["relectura de form_fill"] = relleno
        rutas["informe de needs_value"] = relleno["needs_value"]
        rutas["mensaje del navegador (validationMessage)"] = \
            [r.get("msg", "") for r in relleno["readback"]]
        # Y la página SÍ tiene los tres valores escritos: lo que no sale es la respuesta al modelo, no
        # el relleno. Esto es lo que distingue «no se enseña» de «no se hizo».
        assert await session.page.evaluate(
            "['cuenta', 'doc', 'nac'].map(i => document.getElementById(i).value)") == \
            ["ES9121000418450200051332", "12345678Z", "1990-12-25"], "no se escribió en la página"

        # (c) una segunda tanda: los tres campos vuelven como `not_filled`, y uno de ellos es la fecha,
        #     que es «estructural» y por esa rama sí sale el valor. Con una descripción nueva en medio,
        #     que es el caso peor: la tabla de ids se reacuña y el `sent` de cada campo se pierde, así
        #     que lo único que queda en pie ahí es el tachado de `scrub()`.
        # La página retira su objeción, que es lo que hace un sitio cuando el dato acaba de cuadrarle:
        # sin esto el formulario no valida nunca y no habría envío que medir.
        await session.page.evaluate("document.getElementById('doc').setCustomValidity('')")
        d2 = await forms.describe(session)
        otra = await forms.fill(session, [{"id": fid(d2, "Razón"), "value": "Cambio de domicilio"}])
        rutas["fila not_filled de una segunda tanda"] = otra

        # (d) el envío: la url de aterrizaje lleva los valores en la query y la página los repite como
        #     texto suyo y como identificador de confirmación.
        assert otra["form_valid"] == 1, otra["blocking"]
        token = {t["button"]: t for t in otra["submit_tokens"]}["Enviar solicitud"]["token"]
        enviado = await forms.submit(session, token, approved=True, timeout=15.0)
        assert enviado["status"] == "success", enviado
        assert enviado["confirmation_id"], "la página sí publicó una referencia: hay que mirarla"
        rutas["resultado de form_submit (url, mensaje, confirmación)"] = enviado

        fugas = [(ruta, PERFIL_VALORES[v])
                 for ruta, blob in rutas.items()
                 for v in PERFIL_VALORES if v in json.dumps(blob, ensure_ascii=False)]
        for ruta, que in fugas:
            print(f"   FUGA  {ruta}: {que}")
        print(f"techo de la relectura: {len(fugas)} de {len(rutas) * len(PERFIL_VALORES)} rutas "
              f"llevan un valor del perfil al modelo, sobre {len(rutas)} rutas y "
              f"{len(PERFIL_VALORES)} valores  OK")
        assert not fugas, "un valor del perfil volvió al modelo"
    finally:
        await session.close()
        observe.reset()
        forms.reset()


# --- 6-ter. una sola clasificación por campo --------------------------------------------------------

async def una_sola_clasificacion(base: str) -> None:
    """Las dos cosechas tienen que decir el MISMO nivel del mismo campo.

    `sanitize.classify` lee el nombre accesible, y ese nombre se calculaba dos veces con precedencias
    opuestas (`observe.nameOf` miraba `aria-label` antes de `aria-labelledby`, `forms.labelOf` al
    revés). El mismo campo salía `[secreto:B]` por browser_observe y de nivel C por form_describe, y
    entonces `form_fill` devolvía un número de tarjeta en claro.
    """
    session = await BrowserSession.start(f"{base}/dos-cosechas.html")
    try:
        obs = await observe.observe(session)
        d = await forms.describe(session)
        niveles = {f["nm"]: f.get("sec", "") for f in d["fields"]}
        assert niveles == {"a": "B", "b": "B"}, niveles
        # `al` (la otra etiqueta aria) es una señal del clasificador y NO se emite, igual que `id` y
        # `ph`: es texto de la página y cada clave nueva que sale es una ruta nueva del techo.
        assert not any("al" in f for f in d["fields"]), d["fields"]
        assert obs["secrets"] == d["secrets"] == 2, (obs["secrets"], d["secrets"])
        assert len([l for l in obs["listing"] if "[secreto:B]" in l]) == 2, obs["listing"]
        # El nombre que ve el modelo también es el mismo por las dos tools, y es el de HTML-AAM.
        for f in d["fields"]:
            assert f["from"] == "labelledby", f
            assert f'"{f["label"]}"' in "\n".join(obs["listing"]), (f["label"], obs["listing"])
        # Y la relectura de los dos campos devuelve longitud, no contenido.
        got = await forms.fill(session, [{"id": fid(d, "Referencia del pedido"), "value": TARJETA},
                                         {"id": fid(d, "Contraseña de un solo uso"), "value": OTP}])
        for label, largo in (("Referencia del pedido", len(TARJETA)),
                             ("Contraseña de un solo uso", len(OTP))):
            r = row(got, label)
            assert r["sec"] == "B" and r["len"] == largo and "value" not in r, r
        no_aparece(got, TARJETA, OTP)
        print("una sola clasificación: el mismo campo sale de nivel B por browser_observe y por "
              "form_describe, con el mismo nombre, y la relectura da longitud  OK")
    finally:
        await session.close()
        observe.reset()
        forms.reset()


# --- 6-quater. el traspaso de pago, por el camino positivo ------------------------------------------

async def traspaso_de_pago(port: int) -> None:
    """Un campo dentro de un iframe de pasarela es un TRASPASO, no un relleno, y se detecta por el
    ORIGEN del frame aunque ningún campo lleve `autocomplete=cc-*`.

    La regla del `cc-` cubre el caso fácil; la lista de orígenes cubre el de Stripe, cuyo iframe no
    pone `autocomplete` en nada. De ese camino solo había un test negativo —«ningún campo de este
    formulario es de pago»—, que pasaría igual si la rama no existiera. Aquí se prueba el positivo con
    dos orígenes locales (`localhost` y `127.0.0.1` lo son) y la lista apuntada al segundo.
    """
    session = await BrowserSession.start(f"http://localhost:{port}/ground.html")
    guardado = forms.PAY_HOST
    forms.PAY_HOST = re.compile(r"^127\.0\.0\.1:\d+$")
    try:
        inner = f"http://127.0.0.1:{port}/inner.html"
        await session.page.evaluate(f"document.getElementById('marco').src = {json.dumps(inner)}")
        for _ in range(100):
            marcos = [f for f in session.page.frames if f.url.endswith("inner.html")]
            if marcos:
                await marcos[0].wait_for_load_state()
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("el iframe de otro origen no cargó")

        d = await forms.describe(session)
        cupon = [f for f in d["fields"] if f["label"] == "Cupón"]
        assert len(cupon) == 1, [f["label"] for f in d["fields"]]
        cupon = cupon[0]
        assert cupon.get("pay") == 1 and cupon.get("sec") == "A", cupon
        assert cupon["in"] == f"frame:127.0.0.1:{port}", cupon
        assert "value" not in cupon and cupon["len"] == 0, cupon
        # Y el documento de arriba NO se contagia: marcar el frame principal dejaría al agente sin
        # rellenar ni la dirección de envío de un checkout normal.
        assert [f["label"] for f in d["fields"] if f.get("pay")] == ["Cupón"], \
            [f["label"] for f in d["fields"] if f.get("pay")]

        got = await forms.fill(session, [{"id": cupon["i"], "value": "DESCUENTO-10"}])
        assert [h["reason"] for h in got["human_required"]] == ["payment"], got["human_required"]
        assert "pásaselo al usuario" in got["human_required"][0]["next"]
        # El campo sale en la relectura como `not_filled` —igual que cualquier campo que el agente no
        # escribió— con su longitud 0, su nivel A y sin valor: no se tecleó nada en él.
        traspasado = row(got, "Cupón")
        assert traspasado["verdict"] == "not_filled" and traspasado["sec"] == "A", traspasado
        assert traspasado["len"] == 0 and "value" not in traspasado, traspasado
        assert await marcos[0].evaluate("document.getElementById('cupon').value") == "", \
            "se escribió en un campo de una pasarela de pago"
        print("traspaso de pago: un campo en un iframe de otro origen de la lista sale de nivel A, "
              "sin valor, y form_fill no lo teclea  OK")
    finally:
        forms.PAY_HOST = guardado
        await session.close()
        observe.reset()
        forms.reset()


def limpia_autofill() -> None:
    """Vacía el historial de autocompletado del perfil del agente. Al arrancar y al acabar: lo que se
    asierta de esa tabla tiene que ser lo que escribió esta corrida, no lo que dejó un uso anterior."""
    db = BROWSER_PROFILE / "Default" / "Web Data"
    if not db.exists():
        return
    con = sqlite3.connect(db)
    try:
        con.execute("delete from autofill")
        con.commit()
    finally:
        con.close()


async def main() -> None:
    cv = test_profile()
    test_whitelist()
    limpia_autofill()
    port = serve()
    base = f"http://127.0.0.1:{port}"
    session = await BrowserSession.start(f"{base}/form-es.html")
    try:
        # --- 3. una sola regla del valor ----------------------------------------------------------
        described = await forms.describe(session)
        no_aparece(described, CLAVE, TARJETA)
        # La cosecha de observe y la de forms tienen que decir la MISMA longitud en cada campo. Se
        # comparan por (nombre del campo) porque los dos espacios de ids son distintos a propósito.
        async with forms.Frames(session) as frames:
            crudo = await forms._harvest(frames, base=True)
        vl_forms = {r.get("nm"): r.get("vl") for _f, _h, d in crudo for r in d["fields"] if r.get("nm")}
        vl_obs = {}
        async with forms.Frames(session) as frames:
            for frame_id, _host in frames.order:
                got = await forms._eval1(frames.cdp[frame_id], frame_id, observe.HARVEST,
                                         {"viewport_only": False})
                vl_obs |= {r.get("nm"): r.get("vl") for r in got["els"] if r.get("nm")}
        comunes = {k for k in vl_forms if k in vl_obs and vl_forms[k] is not None}
        assert len(comunes) >= 19, f"solo {len(comunes)} campos de texto libre comparables"
        distintos = {k: (vl_forms[k], vl_obs[k]) for k in comunes if vl_forms[k] != vl_obs[k]}
        assert not distintos, f"las dos cosechas no dicen la misma longitud: {distintos}"
        assert vl_forms["clave"] == len(CLAVE) and vl_forms["tarjeta"] == len(TARJETA)
        print(f"una sola regla del valor: {len(comunes)} campos, misma longitud en las dos "
              f"cosechas  OK")

        # --- la descripción -----------------------------------------------------------------------
        described = await forms.describe(session)
        campos = {f["label"]: f for f in described["fields"]}
        # 28 campos descritos. El reparto por nivel es la aserción que importa, porque de él sale lo
        # que se puede teclear y lo que se puede enseñar: 3 de nivel A (contraseña, tarjeta, CVC) que no
        # se teclean, 2 de nivel B (el OTP y el IBAN) que sí se teclean pero vuelven como longitud, y 23
        # normales. El bucle escribe 25 de los 28.
        assert len(described["fields"]) == 28, \
            f"28 campos, hay {len(described['fields'])}: {sorted(campos)}"
        por_nivel = {}
        for f in described["fields"]:
            por_nivel.setdefault(f.get("sec", ""), []).append(f["label"])
        assert sorted(por_nivel["A"]) == ["CVC", "Contraseña", "Número de tarjeta"], por_nivel["A"]
        assert sorted(por_nivel["B"]) == ["Código de verificación", "IBAN"], por_nivel["B"]
        assert len(por_nivel[""]) == 23, len(por_nivel[""])
        # Y el nivel A es del campo, no del frame: un `autocomplete=cc-number` en la página de arriba no
        # puede marcar el documento entero, o el agente no rellenaría ni la dirección de envío.
        assert not any(f.get("pay") for f in described["fields"])
        assert campos["Sexo"]["k"] == "radio" and campos["Sexo"]["opts_n"] == 3, campos["Sexo"]
        assert len([f for f in described["fields"] if f["k"] == "radio"]) == 1, \
            "un grupo de radios es UN campo, no tres"
        assert campos["Nombre"]["from"] == "label-for"
        assert campos["Recibir boletín"]["from"] == "labelledby"
        assert campos["Fecha de caducidad del DNI"]["from"] == "label-for"
        assert campos["Nombre"]["section"] == "Datos personales"
        assert campos["IBAN"]["section"] == "Dirección" and campos["IBAN"].get("shadow") == 1
        assert campos["Fecha de nacimiento"]["format"] == "AAAA-MM-DD"
        assert campos["Fecha de caducidad del DNI"]["format"] == "dd/mm/aaaa", \
            "un type=text con pattern dd/mm/aaaa es el otro animal y tiene que decirlo"
        assert campos["Curriculum"]["opened_by"] == "Subir CV" and campos["Curriculum"]["hidden"] == 1
        assert campos["DNI"]["pattern"] == "[0-9]{8}[A-Za-z]"
        assert campos["DNI"]["error"] == "Ocho dígitos y la letra, sin guiones"
        # 52 provincias + «elige» se truncan, y se puede consultar por prefijo.
        assert campos["Provincia"]["opts_n"] == 53
        assert len(campos["Provincia"]["options"]) == forms.MAX_OPTS
        murcia = await forms.describe(session, option_prefix="Murc")
        opciones = {f["label"]: f.get("options") for f in murcia["fields"]}["Provincia"]
        assert [o["t"] for o in opciones] == ["Murcia"], opciones
        described = await forms.describe(session)       # la consulta por prefijo reacuña los ids
        # Los botones y sus clasificaciones.
        botones = {b["t"]: b for b in described["buttons"]}
        assert botones["Enviar"]["cls"] == "irreversible"
        assert botones["Guardar borrador"]["cls"] == "draft"
        assert botones["Subir CV"]["cls"] == "opens_file" and botones["Subir CV"]["for"] is not None
        print(f"form_describe: {len(described['fields'])} campos, {described['secrets']} sensibles, "
              f"{len(described['buttons'])} botones clasificados, "
              f"{len(json.dumps(described, ensure_ascii=False).encode())} B  OK")

        # --- la regla del secreto en la descripción -----------------------------------------------
        for label, nivel in (("Contraseña", "A"), ("Número de tarjeta", "A"), ("CVC", "A"),
                             ("Código de verificación", "B")):
            f = campos[label]
            assert f["sec"] == nivel, (label, f)
            assert "value" not in f and "len" in f, f
        assert campos["Contraseña"]["len"] == len(CLAVE)
        assert campos["Número de tarjeta"]["len"] == len(TARJETA)
        # Y ningún campo de texto libre emite su valor, lo reconozca el clasificador o no.
        assert not any("value" in f for f in described["fields"]
                       if f["k"] in ("text", "email", "tel", "password", "textarea", "richtext")), \
            "un campo de texto libre emitió su valor"

        # --- 4. dry_run: la preflight gratis ------------------------------------------------------
        seco = await forms.fill(session, [
            {"id": fid(described, "Fecha de nacimiento"), "value": "25/12/1990"},
            {"id": fid(described, "DNI"), "value": "1234"},
            {"id": fid(described, "País"), "value": "Alemania"},
            {"id": fid(described, "Curriculum"), "value": "/no/existe/cv.pdf"},
            {"id": fid(described, "Nombre"), "value": "{{perfil.nombre}}"},
        ], dry_run=True)
        assert seco["dry_run"] is True
        checks = {p["label"]: p["check"] for p in seco["plan"]}
        assert "AAAA-MM-DD" in checks["Fecha de nacimiento"], checks["Fecha de nacimiento"]
        assert "pattern" in checks["DNI"]
        assert "no es una opción" in checks["País"]
        assert "EN SILENCIO" in checks["Curriculum"]
        assert checks["Nombre"] == "ok"
        # El plan lleva la CLAVE, nunca el valor resuelto: el dry_run no es una ruta de fuga.
        assert [p for p in seco["plan"] if p["label"] == "Nombre"][0]["sent"] == "{{perfil.nombre}}"
        no_aparece(seco, "Lucía")
        # Y la ruta de fuga menos obvia: un `check` que falla CITA el valor, y si el valor venía del
        # perfil el modelo no lo conocía. Un IBAN con un dígito de más contra `pattern=ES[0-9]{22}`
        # tiene que volver tachado, no en claro.
        malo = await forms.fill(session, [{"id": fid(described, "IBAN"),
                                           "value": "{{perfil.iban}}9"}], dry_run=True)
        iban = [p for p in malo["plan"] if p["label"] == "IBAN"][0]
        assert "no cumple pattern" in iban["check"], iban
        assert sanitize.REDACTED in iban["check"], f"el check citó el IBAN en claro: {iban['check']}"
        no_aparece(malo, "ES9121000418450200051332")
        print(f"dry_run: {len(seco['blocked'])} de 5 campos parados sin tocar la página, y el valor de "
              f"un campo sensible sale tachado del mensaje de error  OK")

        # --- 5. los 24 campos, de punta a punta ---------------------------------------------------
        plan = [
            ("Nombre", "{{perfil.nombre}}"),
            ("Primer apellido", "{{perfil.apellido1}}"),
            ("Segundo apellido", "{{perfil.apellido2}}"),
            ("DNI", "{{perfil.dni}}"),
            ("Fecha de nacimiento", "{{perfil.nacimiento}}"),
            ("Fecha de caducidad del DNI", "2031-03-14"),          # ISO -> dd/mm/aaaa, lo hace el servidor
            ("Correo electrónico", "{{perfil.email}}"),
            ("Teléfono", "{{perfil.telefono}}"),                   # +34 fuera, y con máscara
            ("Sexo", "M"),
            ("Idioma de contacto", "Español"),
            ("Calle", "{{perfil.direccion.via}}"),
            ("Número", "{{perfil.direccion.numero}}"),
            ("Piso y puerta", "{{perfil.direccion.piso}}{{perfil.direccion.puerta}}"),
            ("Código postal", "{{perfil.direccion.cp}}"),
            ("Municipio", "Murcia"),                               # combobox ARIA
            ("Provincia", "Murcia"),
            ("País", "ES"),
            ("IBAN", "{{perfil.iban}}"),                           # dentro del shadow root
            ("Curriculum", cv),
            ("Importe domiciliado", "123456"),                     # máscara es-ES
            ("Acepto las condiciones", True),
            ("Recibir boletín", True),                             # div[role=checkbox] sin teclado
            ("Comentarios", "Sin comentarios."),
            ("Biografía", "Nacida en Murcia."),                    # contenteditable
            ("Código de verificación", OTP),                       # nivel B: se teclea, no se enseña
        ]
        relleno = await forms.fill(
            session, [{"id": fid(described, label), "value": value} for label, value in plan],
            filename=str(Path(tempfile.gettempdir()) / "manos-form.jpg"))

        malos = [r for r in relleno["readback"]
                 if r["verdict"] not in ("ok", "mask", "already", "not_filled")]
        for r in malos:
            print(f"   !! {r['label']:30s} {r['verdict']:22s} {r.get('msg', '')[:70]}")
        assert not malos, f"{len(malos)} campos no quedaron bien"
        # De un campo que rellenó el SERVIDOR desde el perfil vuelven la longitud, la validez y la
        # clave, nunca el valor: el modelo mandó una clave y el dato no lo tiene. Lo que le confirma
        # que entró bien es el `verdict`.
        nombre = row(relleno, "Nombre")
        assert nombre["sent"] == "{{perfil.nombre}}" and nombre["len"] == len("Lucía"), nombre
        assert "value" not in nombre, "la relectura enseñó un valor del perfil"
        piso = row(relleno, "Piso y puerta")
        assert piso["len"] == 2 and piso["sent"] == \
            "{{perfil.direccion.piso}}{{perfil.direccion.puerta}}", piso
        nac = row(relleno, "Fecha de nacimiento")
        assert nac["sent"] == "{{perfil.nacimiento}}" and "value" not in nac, nac
        # La fecha que el MODELO trajo sí vuelve renderizada como la leería una persona, no en ISO.
        assert row(relleno, "Fecha de caducidad del DNI")["value"] == "14/03/2031"
        # La máscara reformatea y eso es un éxito, no un fallo. El valor no se ve —viene del perfil—
        # pero la longitud dice que entró entero y el veredicto dice que la página lo reformateó.
        tel = row(relleno, "Teléfono")
        assert tel["verdict"] == "mask" and tel["len"] == len("600 11 22 33"), tel
        assert "value" not in tel, tel
        imp = row(relleno, "Importe domiciliado")
        assert imp["verdict"] == "mask" and imp["value"] == "1234,56 €", imp
        # Un select vuelve por el TEXTO de su opción, no por su valor.
        assert row(relleno, "País")["value"] == "España", row(relleno, "País")
        assert row(relleno, "Provincia")["value"] == "Murcia"
        assert row(relleno, "Idioma de contacto")["value"] == "Español"
        assert row(relleno, "Municipio")["value"] == "Murcia"
        assert row(relleno, "Sexo")["value"] == "Mujer"
        assert row(relleno, "Acepto las condiciones")["value"] == "sí"
        assert row(relleno, "Recibir boletín")["value"] == "sí"
        assert row(relleno, "Curriculum")["value"].startswith("cv.pdf (")
        assert row(relleno, "Biografía")["value"] == "Nacida en Murcia."
        assert relleno["screenshot"]["redacted_fields"] >= 8, relleno["screenshot"]

        # Ningún campo de nivel A se tecleó. El CVC está vacío, así que es un traspaso: el formulario no
        # se puede acabar sin una persona. La contraseña y la tarjeta ya traen valor —lo puso una
        # persona— así que no hay nada que pedir, y vuelven como `not_filled` con su longitud.
        traspaso = {h["label"]: h for h in relleno["human_required"]}
        assert set(traspaso) == {"CVC"}, sorted(traspaso)
        assert traspaso["CVC"]["reason"] == "card_or_otp" and "operación a dos" in traspaso["CVC"]["next"]
        for label, largo in (("Contraseña", len(CLAVE)), ("Número de tarjeta", len(TARJETA))):
            r = row(relleno, label)
            assert r["verdict"] == "not_filled" and r["sec"] == "A" and r["len"] == largo, r
            assert "value" not in r, f"la relectura de «{label}» enseñó el contenido"

        lat = [r["ms"] for r in relleno["readback"] if "ms" in r]
        print(f"form_fill: {len(lat)} campos escritos y verificados en {relleno['ms']:.0f} ms, "
              f"{relleno['ms_per_field']:.0f} ms/campo (mín {min(lat):.0f}, máx {max(lat):.0f}), "
              f"cero viajes al modelo · form_valid={relleno['form_valid']} "
              f"· captura {relleno['screenshot']['bytes']} B con "
              f"{relleno['screenshot']['redacted_fields']} campos tapados  OK")
        tecnicas = {}
        for r in relleno["readback"]:
            if r.get("used"):
                tecnicas.setdefault(r["used"].split("(")[0].strip(), []).append(r["label"])
        for tecnica, cuales in sorted(tecnicas.items(), key=lambda kv: -len(kv[1])):
            print(f"   {len(cuales):2d}  {tecnica}")

        # --- 6. la regla del secreto, por valor, sobre el JSON entero -----------------------------
        otp_row = row(relleno, "Código de verificación")
        assert otp_row["sec"] == "B" and otp_row["len"] == len(OTP), otp_row
        assert "value" not in otp_row, "la relectura de un campo de nivel B enseñó el contenido"
        assert otp_row["valid"] == 1
        # Y los valores del perfil tampoco, por ninguna ruta de esta respuesta: el IBAN va a un campo
        # de nivel B y el DNI, el nombre y los apellidos a campos que el clasificador no marca.
        no_aparece(relleno, CLAVE, TARJETA, OTP,
                   "Lucía", "Ramírez", "12345678Z", "ES9121000418450200051332", "lucia.ramirez")
        # Y el eco: el valor escrito en el nivel B está memorizado, así que `scrub` lo tacha en
        # cualquier otra respuesta, aunque la página lo pinte como texto suyo.
        assert OTP in sanitize.known()
        assert sanitize.scrub({"eco": f"su código es {OTP}"})["eco"] == \
            f"su código es {sanitize.REDACTED}"
        print(f"regla del secreto: contraseña len={campos['Contraseña']['len']}, "
              f"tarjeta len={campos['Número de tarjeta']['len']}, OTP len={otp_row['len']}; "
              f"ninguno de los tres valores aparece en el JSON de ninguna de las tres tools  OK")

        # --- 7. needs_value: un obligatorio que el plan no trae -----------------------------------
        await session.page.reload()
        observe.reset()
        forms.reset()
        d2 = await forms.describe(session)
        corto = await forms.fill(session, [{"id": fid(d2, "Nombre"), "value": "Lucía"}])
        faltan = {n["label"]: n for n in corto["needs_value"]}
        assert "DNI" in faltan and faltan["DNI"]["pattern"] == "[0-9]{8}[A-Za-z]"
        assert faltan["Fecha de nacimiento"]["format"] == "AAAA-MM-DD"
        assert faltan["Provincia"]["options"], "un needs_value de select trae sus opciones"
        assert "No inventes" in faltan["DNI"]["next"]
        assert corto["form_valid"] == 0 and corto["blocking"]
        assert "Contraseña" not in faltan, "un campo de nivel A es human_required, no needs_value"
        print(f"needs_value: {len(faltan)} campos obligatorios sin valor, con sus restricciones  OK")

        # --- 8. un token por botón de envío visible, con su clasificación --------------------------
        # Uno por botón y no solo el primero en orden del DOM: con un «Guardar borrador» delante, el
        # «Enviar» de verdad era inalcanzable por esta tool, y ese es el formulario de dos botones más
        # común que existe.
        fichas = {t["button"]: t for t in corto["submit_tokens"]}
        assert sorted(fichas) == ["Enviar", "Guardar borrador"], sorted(fichas)
        assert fichas["Enviar"]["classification"] == "irreversible"
        assert fichas["Guardar borrador"]["classification"] == "draft"
        assert len({t["token"] for t in corto["submit_tokens"]}) == 2, "dos botones, dos tokens"
        try:
            await forms.submit(session, fichas["Enviar"]["token"], approved=True)
        except Exception as exc:
            raise AssertionError(f"la puerta tenía que negarse, no romperse: {exc!r}")
        cuales = ", ".join(f"{b}={t['classification']}" for b, t in sorted(fichas.items()))
        print(f"tokens: {len(fichas)}, uno por botón de envío visible, con su clasificación "
              f"({cuales})  OK")

    finally:
        await session.close()
        observe.reset()
        forms.reset()

    # --- 9. la entrada controlada: no se asigna `.value` ------------------------------------------
    session = await BrowserSession.start(f"{base}/form-react.html")
    try:
        d = await forms.describe(session)
        got = await forms.fill(session, [
            {"id": fid(d, "Nombre controlado"), "value": "diego"},
            {"id": fid(d, "Provincia controlada"), "value": "Murcia"}])
        assert row(got, "Nombre controlado")["verdict"] in ("ok", "mask")
        # El espejo del componente es la prueba: solo cambia si el manejador corrió de verdad. Con
        # `el.value = x` el rastreador de valor descarta el evento y el estado se queda vacío.
        estado = await session.page.evaluate("document.getElementById('estado').textContent")
        sestado = await session.page.evaluate("document.getElementById('sestado').textContent")
        assert estado == "DIEGO", (f"el estado del componente es {estado!r}: se escribió con una "
                                   "asignación de .value y la divergencia corrompe el siguiente clic")
        assert sestado == "MU", f"el onChange del select no saltó: estado {sestado!r}"
        assert row(got, "Provincia controlada")["value"] == "Murcia"
        print(f"entrada controlada: DOM y estado del componente de acuerdo "
              f"(espejo {estado!r}, select {sestado!r})  OK")
    finally:
        await session.close()
        observe.reset()
        forms.reset()

    # --- 10. la puerta previa al envío, el token de un solo uso y el envío de verdad --------------
    session = await BrowserSession.start(f"{base}/form-paso.html")
    try:
        d = await forms.describe(session)
        etiquetas = {f["label"]: f for f in d["fields"]}
        assert etiquetas["Referencia interna"]["from"] == "nearby", \
            "un campo sin etiqueta cae a `nearby` y se marca de confianza baja"
        assert etiquetas["Motivo de la solicitud"]["hidden"] == 1
        cls = {b["t"]: b["cls"] for b in d["buttons"]}
        assert cls == {"Siguiente": "step", "Guardar": "draft", "Enviar solicitud": "irreversible",
                       "Confirmar pedido": "irreversible"}, cls

        paso1 = await forms.fill(session, [{"id": fid(d, "Correo electrónico"),
                                            "value": "{{perfil.email}}"}])
        # El paso 2 se reveló, y el obligatorio que trae BLOQUEA aunque no se vea.
        assert "Motivo de la solicitud|motivo|text" in paso1["changed_fields"]["appeared"], \
            paso1["changed_fields"]
        assert paso1["form_valid"] == 0
        bloquean = {b[0]: b for b in paso1["blocking"]}
        assert "Motivo de la solicitud" in bloquean, paso1["blocking"]
        assert bloquean["Motivo de la solicitud"][1] == "valueMissing"
        print(f"puerta previa al envío: pilló «Motivo de la solicitud», obligatorio dentro de un paso "
              f"que estaba oculto al describir  OK")

        # Un formulario inválido no se envía, y se dice qué lo bloquea.
        negado = await forms.submit(session, paso1["submit_tokens"][0]["token"], approved=True)
        assert negado["status"] == "refused" and negado["errors"], negado
        assert await session.page.evaluate("location.pathname") == "/form-paso.html"

        d = await forms.describe(session)
        paso2 = await forms.fill(session, [{"id": fid(d, "Motivo de la solicitud"),
                                            "value": "Cambio de domicilio"}])
        assert paso2["form_valid"] == 1, paso2["blocking"]
        # Los cuatro botones de envío, cuatro tokens. Y la clasificación es conservadora por diseño:
        # con el paso 2 ya revelado y relleno no queda ningún obligatorio escondido, así que
        # `Siguiente` ya no se puede demostrar que sea un paso intermedio y vuelve a ser irreversible.
        # Por defecto todo botón de envío lo es.
        fichas = {t["button"]: t for t in paso2["submit_tokens"]}
        assert {b: t["classification"] for b, t in fichas.items()} == {
            "Siguiente": "irreversible", "Guardar": "draft",
            "Enviar solicitud": "irreversible", "Confirmar pedido": "irreversible"}, fichas

        # Un token irreversible se niega la primera vez y NO se gasta: es el agujero que el token tapa.
        # Y es el token del CUARTO botón del DOM, que antes no se podía acuñar.
        irr = fichas["Confirmar pedido"]
        pausa = await forms.submit(session, irr["token"])
        assert pausa["status"] == "needs_approval", pausa
        assert "Confirmar pedido" in pausa["next"] and "approved=true" in pausa["next"]
        assert await session.page.evaluate("location.pathname") == "/form-paso.html", \
            "un token irreversible sin aprobar no puede haber pulsado nada"
        assert irr["token"] in forms._tokens, "el token se gastó en la negativa"
        print(f"token irreversible: negado sin aprobar, sin pulsar nada y sin gastarse  OK")

        # Y el envío de verdad, con su cadena de evidencias. Se pulsa «Enviar solicitud», que es el
        # TERCERO en orden del DOM: acuñar solo el primero hacía inalcanzable el botón de verdad.
        d = await forms.describe(session)
        relleno = await forms.fill(session, [{"id": fid(d, "Referencia interna"), "value": "REF-1"}])
        token = {t["button"]: t for t in relleno["submit_tokens"]}["Enviar solicitud"]["token"]
        enviado = await forms.submit(session, token, approved=True, timeout=15.0)
        assert enviado["status"] == "success", enviado
        assert enviado["http_status"] == 303, enviado
        assert enviado["confirmation_id"] == "ES-2026-88421", enviado
        assert "recibido su solicitud" in enviado["message"], enviado
        print(f"form_submit: {enviado['status']} · http {enviado['http_status']} · "
              f"confirmación {enviado['confirmation_id']} · «{enviado['message']}»  OK")

        # De un solo uso: el mismo token no vale dos veces.
        try:
            await forms.submit(session, token, approved=True)
        except forms.FormError as exc:
            assert "un solo uso" in str(exc), exc
        else:
            raise AssertionError("un submit_token tenía que ser de un solo uso")
        # Y un id de campo de antes del envío está muerto, nunca escribe en el campo equivocado.
        try:
            await forms.fill(session, [{"id": 0, "value": "x"}])
        except forms.StaleHandle as exc:
            assert "form_describe" in str(exc)
        else:
            raise AssertionError("tras un envío los ids de campo tenían que morir")
        print("token de un solo uso y StaleHandle tras el envío  OK")

        # --- el freno del doble envío --------------------------------------------------------------
        # El mismo formulario, los mismos valores, otra vez. La clave es (acción, huella de lo
        # relleno) y NO caduca con la página, así que volver a la página y rellenar igual tiene que
        # chocar con el freno. Antes no frenaba nunca: `reset()` borraba la tabla, y `submit()` llama a
        # `reset()` justo después de enviar.
        assert forms._submitted, "el envío no apuntó nada en la tabla de envíos recientes"
        await session.page.goto(f"{base}/form-paso.html")
        observe.reset()
        forms.reset()
        d = await forms.describe(session)
        await forms.fill(session, [{"id": fid(d, "Correo electrónico"), "value": "{{perfil.email}}"},
                                   {"id": fid(d, "Referencia interna"), "value": "REF-1"}])
        d = await forms.describe(session)
        otra = await forms.fill(session, [{"id": fid(d, "Motivo de la solicitud"),
                                           "value": "Cambio de domicilio"}])
        assert otra["form_valid"] == 1, otra["blocking"]
        repe = {t["button"]: t for t in otra["submit_tokens"]}["Enviar solicitud"]["token"]
        try:
            res = await forms.submit(session, repe, approved=True, timeout=15.0)
        except forms.FormError as exc:
            assert "menos de" in str(exc) and "dos veces" in str(exc), exc
        else:
            raise AssertionError(f"el mismo envío pasó dos veces: {res}")
        assert await session.page.evaluate("location.pathname") == "/form-paso.html", \
            "el segundo envío pulsó algo"
        print("freno del doble envío: el mismo formulario con los mismos valores se niega a los "
              f"{forms.DOUBLE_SUBMIT:.0f} s, y no se pulsó nada  OK")
    finally:
        await session.close()
        observe.reset()
        forms.reset()

    # --- lo que el envío deja en disco, que es el precio del pref del Autofill --------------------
    # `browser/session.py` siembra `autofill.profile_enabled = True` porque sin él `Autofill.trigger`
    # no funciona en ningún campo de ningún formulario. El precio, medido con el pref a False y a True:
    # enviar un formulario escribe el historial de autocompletado por campo en el perfil. Las
    # contraseñas no entran y las tarjetas tampoco, pero **un OTP tecleado en un `type=text` sí**. Se
    # asierta aquí para que el día que ese precio cambie —en los dos sentidos— lo diga un test y no una
    # sorpresa, y se limpia después porque son valores de un fixture.
    db = BROWSER_PROFILE / "Default" / "Web Data"
    con = sqlite3.connect(db)
    try:
        # Filtrado a los nombres que produce ESTE fixture: la tabla es persistente y del perfil real
        # del agente, así que cualquier otro uso del navegador entre corridas la contamina —y la
        # contaminó, con un `n=Murcia` de una página sin relación—. La tabla se vacía además al
        # arrancar el test, así que lo que quede con estos tres nombres lo escribió esta corrida.
        mios = ("correo", "motivo", "suelto")
        dejado = sorted((n, v) for n, v in con.execute("select name, value from autofill")
                        if n in mios)
        assert [n for n, _v in dejado] == list(mios), dejado
        assert con.execute("select count(*) from addresses").fetchone()[0] == 0, \
            "el Autofill de CDP no guarda direcciones en el perfil, y si empieza a hacerlo hay que saberlo"
        assert con.execute("select count(*) from credit_cards").fetchone()[0] == 0
        con.execute("delete from autofill")
        con.commit()
        print(f"precio del pref del Autofill: enviar dejó {len(dejado)} filas de historial por campo en "
              f"el perfil ({', '.join(n for n, _v in dejado)}), 0 direcciones y 0 tarjetas; limpiado  OK")
    finally:
        con.close()

    await una_sola_clasificacion(base)
    await traspaso_de_pago(port)
    await techo_del_perfil(base)

    # --- 11. el Autofill de Chrome -----------------------------------------------------------------
    session = await BrowserSession.start(f"{base}/form-es.html")
    try:
        d = await forms.describe(session)
        # El agente teclea la calle con eventos de confianza...
        await forms.fill(session, [{"id": fid(d, "Calle"), "value": "Calle Falsa 123"}])
        # ...y el código postal llega pre-rellenado por la página, que es lo que simula esta asignación
        # (es lo que hizo el sondeo, y aquí vale porque es el test haciendo de página; el módulo no
        # asigna `.value` nunca). Con los dos puestos, el diff del Autofill tiene las dos mitades.
        await session.page.evaluate("document.getElementById('cp').value = '11111'")
        d = await forms.describe(session)
        auto = await forms.fill(session, [{"id": fid(d, "Comentarios"), "value": "con autofill"}],
                                autofill_profile=True)
        report = auto["autofill"]
        assert report is not None
        if not report.get("ok"):
            raise AssertionError(f"el Autofill no corrió: {report}")
        clasificados = {n: t for n, t, _s in report["classified"]}
        assert len(clasificados) >= 10, clasificados
        assert clasificados.get("nombre") == "First name", clasificados
        assert clasificados.get("cp") == "ZIP code", clasificados
        assert all(n in forms.AUTOFILL_SAFE for n in report["names_sent"])
        # `addressUi` es un espejo del perfil, no un informe de la página: no sale. Y como el nivel A no
        # se puede meter en un payload de Autofill, el único valor que podría salir es de nivel C.
        no_aparece(report, CLAVE, TARJETA, "addressUi")
        # El diff de sobrescritura, y una distinción medida aquí que el sondeo no separaba: el Autofill
        # pisa el valor que puso la página (`cp`: 11111 -> 30001) y **no** pisa el que escribió el
        # agente con eventos de confianza (`via` sigue siendo 'Calle Falsa 123'). Las dos mitades
        # importan: la primera es la razón de que el diff tenga que existir, y la segunda es la razón de
        # que lanzar el Autofill sobre un formulario a medias sea menos grave de lo que parecía.
        assert "cp" in report["overwrote"], report["overwrote"]
        assert "via" not in report["overwrote"], report["overwrote"]
        assert row(auto, "Calle")["len"] == len("Calle Falsa 123")
        rellenos = [r for r in auto["readback"]
                    if r["verdict"] == "not_filled" and r.get("value") not in (None, "", "no")]
        print(f"Autofill: UNA llamada, {len(clasificados)} campos clasificados por Chrome, "
              f"{len(report['overwrote'])} sobrescritos sin avisar ({report['overwrote']}), "
              f"{len(rellenos)} campos con valor que el bucle no escribió  OK")
        print(f"   tipos de Chrome: "
              f"{', '.join(f'{n}={t}' for n, t in list(clasificados.items())[:6])}…")
    finally:
        await session.close()
        observe.reset()
        forms.reset()

    # --- 12. la caída inducida: la whitelist es la defensa, y de la caída se vuelve reabriendo ------
    # Esto mata el proceso del navegador a propósito, con uno de los 10 nombres medidos. Va al final
    # porque después no hay navegador. `Autofill.trigger` NO VUELVE cuando mata: sin el wait_for, este
    # await se quedaría colgado para siempre con el candado de la sesión cogido.
    # La caída es a propósito: RLIMIT_CORE=1 solo para este árbol, o deja un core por pasada
    # (con 0 systemd-coredump lo apunta igual; docs/knowledge.md).
    core = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (1, core[1]))
    try:
        session = await BrowserSession.start(f"{base}/form-es.html")
    finally:
        resource.setrlimit(resource.RLIMIT_CORE, core)
    procesos_antes = 0
    try:
        d = await forms.describe(session)
        procesos_antes = len(session.cgroup.joinpath("cgroup.procs").read_text().split())
        async with forms.Frames(session) as frames:
            frame_id, _host = frames.order[0]
            cdp = frames.cdp[frame_id]
            await cdp.send("DOM.enable")
            await cdp.send("Autofill.enable")
            node = await cdp.send("DOM.describeNode",
                                  {"objectId": await forms._object_id(cdp, frame_id, 0)})
            t0 = time.perf_counter()
            colgado = False
            try:
                await asyncio.wait_for(cdp.send("Autofill.trigger", {
                    "fieldId": node["node"]["backendNodeId"], "frameId": frame_id,
                    "address": {"fields": [{"name": "PHONE_HOME_CITY_AND_NUMBER",
                                            "value": "600112233"}]}}), forms.AUTOFILL_TIMEOUT)
            except asyncio.TimeoutError:
                colgado = True
        espera = time.perf_counter() - t0
        assert colgado, ("PHONE_HOME_CITY_AND_NUMBER ya no mata el navegador en este binario: vuelve a "
                         "correr research/p2/sweep.py antes de tocar la whitelist")
        assert forms.crashed(session), "el navegador tenía que estar muerto"
        try:
            await forms.after_crash(session)
        except forms.BrowserCrashed as exc:
            assert "browser_close" in str(exc) and "autofill_profile" in str(exc), exc
        else:
            raise AssertionError("una caída del navegador tenía que ser un BrowserCrashed")
        procesos_despues = (len(session.cgroup.joinpath("cgroup.procs").read_text().split())
                            if session.cgroup.exists() else 0)
        print(f"caída inducida: wait_for cortó a los {espera:.1f} s (sin él no vuelve nunca), "
              f"{procesos_antes} procesos -> {procesos_despues}, cgroup "
              f"{'existe' if session.cgroup.exists() else 'borrado'}, BrowserCrashed limpio  OK")
    finally:
        await session.close()
        observe.reset()
        forms.reset()

    # La única recuperación que funciona: cerrar y abrir de cero. `session.recover()` no sirve, porque
    # está escrita para una caída del renderer y aquí murió el navegador entero.
    session = await BrowserSession.start(f"{base}/form-es.html")
    try:
        d = await forms.describe(session)
        assert len(d["fields"]) == 28
        print(f"recuperación: sesión nueva, {len(d['fields'])} campos descritos, "
              f"{len(session.cgroup.joinpath('cgroup.procs').read_text().split())} procesos  OK")
    finally:
        await session.close()
        observe.reset()
        forms.reset()
        forms.forget_submits()
        limpia_autofill()   # los fixtures de este test no dejan historial en el perfil del agente


asyncio.run(main())
print("\ntest_forms: OK")
