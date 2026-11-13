"""Formularios: describir, rellenar y verificar, enviar. Tres tools y nada más (PLAN §5).

El bucle de rellenar y verificar corre **entero en el servidor**: el modelo manda un plan, el servidor
ejecuta las escaleras por tipo de campo con su relectura, y el modelo recibe una respuesta. Medido en el
estudio: 14 campos con relectura y validez por campo en 1,55 s, 111 ms/campo, cero viajes al modelo. Las
decisiones de reintento son mecánicas y un bucle de 40 líneas las toma mejor que el modelo.

Lo que este módulo no vuelve a discutir, cada punto medido:

1. **Nunca se asigna `.value` ni `.checked`.** En React 18, `el.value='Diego'` más un
   `new Event('input')` deja el estado del componente vacío, y la divergencia hace que el siguiente clic
   real calcule el valor equivocado: corrupción de datos en silencio. Se teclea con `Input.insertText`
   (confiable) y, cuando hay que asignar, por el setter del prototipo nativo, que esquiva el rastreador
   de valor de React.
2. **En `date/time/month/week/datetime-local/color/range` no se puede teclear**: `insertText` dio `''` y
   tecla a tecla dio `'121990-02-05'`. Solo el ISO por el setter nativo más eventos.
3. **El desplegable de un `<select>` nativo es UI del navegador**: los rects de sus `<option>` son 0x0 y
   ningún evento de ratón los alcanza. Se conduce por selección en el DOM más eventos.
4. **`DOM.setFileInputFiles` con una ruta inexistente tiene éxito en silencio** (`size 0`) y dos ficheros
   en un input sin `multiple` se queda con el primero sin avisar. Los dos se rechazan aquí antes de la
   llamada, porque CDP no los va a rechazar.
5. **La validez la dice la API de validación de restricciones**, no el árbol AX, cuya propiedad `invalid`
   está mal para los inputs de texto.
6. **Una whitelist de 54 nombres para el Autofill**, porque 10 nombres matan el proceso del navegador
   (SIGTRAP, exit −5) y de esa caída no se vuelve: hay que arrancar una sesión nueva.

**La regla del secreto, que es la razón de que este módulo esté escrito así.** `browser_observe` nunca
emite el valor de un campo de texto libre: emite la longitud. Un formulario se diferencia en una sola
cosa: `form_fill` **sabe lo que acaba de escribir**, así que su relectura es el único sitio legítimo
donde un valor vuelve, para que una persona pueda confirmarlo antes de enviar. Con dos excepciones:

- De un campo de **nivel A** (`type=password`, `autocomplete` de tarjeta o de OTP, o un iframe de
  pasarela) no se lee el contenido **y tampoco se teclea**: contraseñas, tarjetas, CVC y códigos de un
  solo uso no los escribe el agente nunca. Se devuelve `human_required` con el formulario relleno
  alrededor. Una compra o una transferencia es siempre una operación a dos.
- De un campo de **nivel B** (sospecha por la etiqueta, el `aria-label`, el `name`, el `id` o el
  `placeholder`) sí se teclea —el nivel B sanea y escala, nunca bloquea— pero su relectura devuelve la
  longitud y la validez, y el valor escrito pasa por `sanitize.remember()` para que `scrub()` tache sus
  ecos en cualquier otra respuesta.
- Y la tercera, que no depende de ningún clasificador: **de un campo que el servidor rellenó desde
  `{{perfil.clave}}` tampoco sale el valor**, lo marque el clasificador o no. Lo que vuelve es la
  longitud, la validez y la clave (`sent`), porque el modelo mandó una clave y nunca tuvo el dato. La
  excepción de la relectura existe porque el servidor sabe lo que acaba de escribir **y el modelo
  también**; cuando el valor lo puso el perfil, el modelo no lo sabe y enseñárselo es la fuga, no la
  confirmación.

Y **la regla del valor es una sola en todo el código**: la cosecha de aquí emite `vl` (la longitud) y
manda el valor en `fv` hasta `sanitize.classify_rows`, exactamente igual que `observe.HARVEST`.
`tests/test_forms.py` asierta que las dos cosechas dicen el mismo `vl` campo por campo.

Autocomprobación:  uv run --project . tests/test_forms.py
"""

import asyncio
import difflib
import hashlib
import json
import os
import re
import secrets as _nonce
import time
import tomllib
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from browser import observe, sanitize
from browser.session import Cancelled, UserDriving, check, forbidden
from refs import StaleHandle

# `MANOS_PROFILE` existe por el banco de pruebas: sus seis tareas siembran un perfil de laboratorio y
# el verificador lo lee de ahí, así que sin override el agente rellenaría el formulario de pruebas con
# los datos personales de verdad del usuario. Sin la variable, el sitio de siempre.
PROFILE_PATH = Path(os.environ.get("MANOS_PROFILE")
                    or Path.home() / ".config/manos/profile.toml")
MAX_OPTS = 50            # past this the listing truncates and the model queries by prefix
TOKEN_TTL = 600.0        # seconds; the study's 10-minute expiry
AUTOFILL_TIMEOUT = 10.0  # a crashing name never returns through Playwright: this is the only way out
SETTLE = 0.05            # seconds between writing and reading back, as the study's loop measured
ATTEMPTS = 3             # three per field, then a hard stop: the loop never loops forever
DOUBLE_SUBMIT = 180.0    # a repeat of the same action+values inside this window is refused

# Payment origins: a field in one of these is a handoff, never a fill (PLAN §5, skills/forms.md).
PAY_HOST = re.compile(r"(^|\.)(js\.stripe\.com|pay\.google\.com|paypal\.com|redsys\.es)$"
                      r"|^checkoutshopper.*\.adyen\.com$", re.I)

# Field kinds, grouped by the technique ladder that fills them.
DATEISH = frozenset({"date", "time", "month", "week", "datetime-local", "color", "range"})
TOGGLE = frozenset({"checkbox", "radio", "switch"})
SELECTISH = frozenset({"select", "multiselect"})
ISO_FORMAT = {"date": "AAAA-MM-DD", "time": "HH:MM", "month": "AAAA-MM", "week": "AAAA-W##",
              "datetime-local": "AAAA-MM-DDTHH:MM"}
# A text input that wants the local format. Spanish public administration is full of them.
LOCAL_DATE = re.compile(r"dd/mm/a+|dd/mm/yyyy|\\d\{2\}/\\d\{2\}/\\d\{4\}", re.I)
PHONE = re.compile(r"tel[eé]fono|m[oó]vil|phone|whatsapp", re.I)
INTL = re.compile(r"^(\+34|0034)\s*")


class FormError(RuntimeError):
    """Un fallo de formulario con un mensaje que nombra el peldaño siguiente, y es para el modelo."""


class PathRefused(RuntimeError):
    """Una ruta que manos no sube ni abre. `next` va con la excepción."""

    next = ("Esa ruta no se sube ni se abre: ni carpetas ocultas de la casa (~/.ssh, ~/.config, ~/.claude…), ni "
            "/proc, /sys, /dev, ni el perfil del navegador. Usa la ruta absoluta de un fichero normal; si hace falta "
            "ese, pídeselo a Diego.")


def safe_file(path) -> str:
    """La única puerta de una ruta hacia el navegador (subidas de form_fill y browser_act, y `file://`): absoluta,
    fuera de lo vedado (`browser.session.forbidden`, la misma regla que las aserciones) y fichero regular.
    Devuelve la ruta real."""
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        raise ValueError(f"{path}: hace falta una ruta absoluta (una relativa se resolvería contra el servidor).")
    if forbidden(p):
        raise PathRefused(f"{path} está en una carpeta vedada.")
    real = Path(os.path.realpath(p))
    if not real.is_file():
        raise ValueError(f"{path} no existe o no es un fichero regular: una ruta inexistente la acepta el navegador "
                         "EN SILENCIO, con un fichero de 0 bytes.")
    return str(real)


class ProfileError(RuntimeError):
    """El perfil no se carga a medias: o entero o ninguno."""


class BrowserCrashed(RuntimeError):
    """El navegador murió (el Autofill puede matarlo). El estado de la página se ha perdido."""


# --- el perfil del usuario --------------------------------------------------------------------------
# Juego de claves CERRADO: una clave desconocida no se ignora, porque ignorarla convierte una errata en
# un campo silenciosamente vacío. El fichero no lleva secretos, y el detector de «pinta de secreto» es
# el mismo regex del nivel B del saneado aplicado al NOMBRE de la clave: no se escribe un segundo.

PROFILE_KEYS = ("nombre", "apellido1", "apellido2", "dni", "nacimiento", "telefono", "email", "iban",
                "cv", "direccion.via", "direccion.numero", "direccion.piso", "direccion.puerta",
                "direccion.cp", "direccion.municipio", "direccion.provincia", "direccion.pais")



def _looks_secret(key: str) -> bool:
    """Si el NOMBRE de una clave de perfil tiene pinta de secreto, con el regex del nivel B del saneado
    y no con un segundo detector.

    La única sutileza, y está medida: `TIER_B` se calibró contra el TEXTO de una página, donde una
    tarjeta se llama «Número de tarjeta» —eso empareja—, pero una clave de perfil es un sustantivo
    pelado y «tarjeta» a secas no. Así que se le hace la misma pregunta dos veces: por la clave y por la
    clave como la etiquetaría un formulario. Y si aun así se escapa un sustantivo raro no pasa nada
    grave: el juego de claves es cerrado, así que una clave desconocida **no se carga** de todos modos;
    lo único que cambia es qué mensaje de los dos se lee.
    """
    plain = re.sub(r"[_.\-]+", " ", key)
    return bool(sanitize.TIER_B.search(plain) or sanitize.TIER_B.search(f"número de {plain}"))
DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"

_profile: dict[str, str] = {}


def _flat(node, prefix=""):
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _flat(value, f"{dotted}.")
        else:
            yield dotted, value


def _line_of(text: str, key: str) -> int:
    leaf = key.rsplit(".", 1)[-1]
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(leaf):
            return n
    return 0


def _dni_ok(dni: str) -> bool:
    body = dni.strip().upper().replace("-", "")
    body = {"X": "0", "Y": "1", "Z": "2"}.get(body[:1], body[:1]) + body[1:]
    return (len(body) == 9 and body[:8].isdigit()
            and body[8] == DNI_LETTERS[int(body[:8]) % 23])


def load_profile(path: Path = PROFILE_PATH) -> dict:
    """Carga `~/.config/manos/profile.toml`. Todo o nada, y nunca lo escribe.

    Devuelve **las claves, nunca los valores**: el modelo planifica con claves y el servidor rellena con
    valores. Una clave desconocida con pinta de secreto se rechaza con su propio mensaje, porque ese
    fichero no lleva secretos.
    """
    global _profile
    _profile = {}
    if not path.exists():
        return {"keys": [], "warning": f"no hay perfil en {path}; cópialo de profile.example.toml"}
    raw = path.read_text()
    try:
        data = dict(_flat(tomllib.loads(raw)))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path.name}: el TOML no se puede leer ({exc}). No se ha cargado ningún "
                           "valor del fichero.") from None
    for key, value in data.items():
        where = f"{path.name}:{_line_of(raw, key)}"
        if key not in PROFILE_KEYS:
            if _looks_secret(key):
                raise ProfileError(
                    f"{where} — la clave «{key}» tiene pinta de secreto, y este fichero no lleva "
                    "secretos. Quita esa línea. Las contraseñas, los números de tarjeta, los CVC y los "
                    "códigos de un solo uso los teclea una persona en la ventana del agente "
                    "(browser_handoff o Meta+Shift+B) y no se guardan en ningún sitio. No se ha cargado "
                    "ningún valor.")
            near = difflib.get_close_matches(key, PROFILE_KEYS, 1, 0.6)
            raise ProfileError(f"{where} — la clave «{key}» no existe en el perfil."
                               + (f" ¿Querías decir «{near[0]}»?" if near else "")
                               + " Las claves válidas están en profile.example.toml. No se ha cargado "
                                 "ningún valor del fichero.")
        if not isinstance(value, str):
            raise ProfileError(f"{where} — «{key}» tiene que ser una cadena entre comillas, no "
                               f"{type(value).__name__}. No se ha cargado ningún valor del fichero.")
    if "nacimiento" in data:
        try:
            datetime.strptime(data["nacimiento"], "%Y-%m-%d")
        except ValueError:
            raise ProfileError(f"{path.name}:{_line_of(raw, 'nacimiento')} — «nacimiento» va en ISO "
                               "(AAAA-MM-DD), aunque aquí las fechas se escriban día/mes/año. No se ha "
                               "cargado ningún valor del fichero.") from None
    if data.get("dni") and not _dni_ok(data["dni"]):
        raise ProfileError(f"{path.name}:{_line_of(raw, 'dni')} — la letra de control de "
                           f"«{data['dni']}» no cuadra. Un `pattern` de DNI falla después y sin decir "
                           "por qué, así que se comprueba aquí. No se ha cargado ningún valor.")
    _profile = dict(data)
    mode = path.stat().st_mode
    return {"keys": sorted(_profile),
            "warning": (f"aviso: {path} es legible por otros usuarios (modo {mode & 0o777:o}). "
                        "chmod 600 lo arregla." if mode & 0o077 else "")}


_REF = re.compile(r"\{\{\s*perfil\.([a-z0-9_.]+)\s*\}\}", re.I)


def _derived(value: str) -> list[str]:
    """Las formas que el servidor deriva de un valor del perfil antes de escribirlo o de mostrarlo: la
    fecha ISO como la lee una persona y el teléfono sin el prefijo internacional (las dos de `_wanted`
    y `_render`). Se memorizan con el valor porque `scrub()` tacha cadenas exactas, y una derivación es
    el mismo dato con otra forma: el `check` de un `dry_run` cita la forma derivada, no la del fichero.
    """
    out = []
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        out.append(datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y"))
    if INTL.match(value):
        out.append(INTL.sub("", value))
    return out


def resolve(value):
    """Sustituye `{{perfil.clave}}` por su valor. Varias referencias en una cadena se componen, que es
    cómo se rellena un formulario con un solo campo «Nombre y apellidos» o «Dirección».

    **MEMORIZA LO QUE EL MODELO NO SABE; NUNCA MEMORICES LO QUE SÍ SABE.** Es la regla que hay que
    tener delante cada vez que se toca `sanitize.remember()`, porque es la que se ha equivocado dos
    veces en dos sentidos opuestos. Aquí se memoriza **todo** valor que sustituya el servidor, marcado
    el campo o no: el modelo escribió una clave, no un dato, así que cualquier respuesta que le eche el
    dato de vuelta —una comprobación de `dry_run` que cita el valor, un `validationMessage` que la
    página espeja, la url de la página de gracias— le está enseñando algo que no tenía. La regresión de
    la ronda anterior fue la contraria: memorizar lo que el **modelo** teclea, y entonces `scrub()`
    tachaba «Murcia» en el nombre de un enlace legítimo. Esto no puede reabrirla.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value

    def one(match):
        key = match.group(1).lower()
        if key not in PROFILE_KEYS:
            raise FormError(f"«{match.group(0)}» no es una clave del perfil. Las que hay: "
                            f"{', '.join(PROFILE_KEYS)}.")
        if key not in _profile:
            raise FormError(f"el perfil no tiene «{key}»: está en profile.example.toml pero no en "
                            f"{PROFILE_PATH}. Pídeselo al usuario o quita ese campo del plan.")
        sanitize.remember([_profile[key], *_derived(_profile[key])])
        return _profile[key]

    return _REF.sub(one, value).strip()


# --- Autofill de Chrome ------------------------------------------------------------------------------
# 54 nombres aceptados, 0 de ellos mortales, medidos de uno en uno sobre Chrome for Testing
# 153.0.8010.12 (research/p2/E-autofill.md §4). Es una allowlist y eso es el punto: ni el perfil ni el
# modelo pueden meter una cadena en este parámetro sin pasar por aquí. Los 10 que matan el proceso son
# todos los `PHONE_HOME_*` menos `PHONE_HOME_WHOLE_NUMBER`, más `USERNAME_AND_EMAIL_ADDRESS`, y el
# valor da igual: el nombre solo es el peligro, que es justo lo que hace suficiente a una whitelist.

AUTOFILL_SAFE = frozenset({
    "NAME_FIRST", "NAME_MIDDLE", "NAME_LAST", "NAME_MIDDLE_INITIAL", "NAME_FULL", "NAME_SUFFIX",
    "NAME_LAST_FIRST", "NAME_LAST_CONJUNCTION", "NAME_LAST_SECOND", "NAME_HONORIFIC_PREFIX",
    "ALTERNATIVE_FULL_NAME", "ALTERNATIVE_GIVEN_NAME", "ALTERNATIVE_FAMILY_NAME",
    "EMAIL_ADDRESS", "EMAIL_OR_LOYALTY_MEMBERSHIP_ID", "COMPANY_NAME",
    "PHONE_HOME_WHOLE_NUMBER",
    "ADDRESS_HOME_LINE1", "ADDRESS_HOME_LINE2", "ADDRESS_HOME_LINE3",
    "ADDRESS_HOME_STREET_ADDRESS", "ADDRESS_HOME_STREET_NAME", "ADDRESS_HOME_HOUSE_NUMBER",
    "ADDRESS_HOME_HOUSE_NUMBER_AND_APT", "ADDRESS_HOME_APT", "ADDRESS_HOME_APT_NUM",
    "ADDRESS_HOME_APT_TYPE", "ADDRESS_HOME_FLOOR", "ADDRESS_HOME_SUBPREMISE",
    "ADDRESS_HOME_OTHER_SUBUNIT", "ADDRESS_HOME_CITY", "ADDRESS_HOME_STATE", "ADDRESS_HOME_ZIP",
    "ADDRESS_HOME_ZIP_PREFIX", "ADDRESS_HOME_ZIP_SUFFIX", "ADDRESS_HOME_ZIP_AND_CITY",
    "ADDRESS_HOME_COUNTRY", "ADDRESS_HOME_SORTING_CODE", "ADDRESS_HOME_DEPENDENT_LOCALITY",
    "ADDRESS_HOME_DEPENDENT_LOCALITY_AND_LANDMARK", "ADDRESS_HOME_ADDRESS",
    "ADDRESS_HOME_ADDRESS_WITH_NAME", "ADDRESS_HOME_ADMIN_LEVEL2", "ADDRESS_HOME_OVERFLOW",
    "ADDRESS_HOME_OVERFLOW_AND_LANDMARK", "ADDRESS_HOME_LANDMARK",
    "ADDRESS_HOME_STREET_LOCATION", "ADDRESS_HOME_STREET_LOCATION_AND_LOCALITY",
    "ADDRESS_HOME_STREET_LOCATION_AND_LANDMARK", "ADDRESS_HOME_BETWEEN_STREETS",
    "ADDRESS_HOME_BETWEEN_STREETS_1", "ADDRESS_HOME_BETWEEN_STREETS_2",
    "ADDRESS_HOME_BETWEEN_STREETS_OR_LANDMARK", "DELIVERY_INSTRUCTIONS",
})
# The 12 that actually filled the Spanish fixture, which is the practical profile schema. The `card`
# parameter stays wired to nothing on purpose: every secret name (PASSWORD, ONE_TIME_CODE,
# CREDIT_CARD_*, IBAN_VALUE, NATIONAL_ID_CARD_NUMBER) is rejected by this binary anyway, so the
# allowlist is tier-C data by construction.
AUTOFILL_MAP = (("nombre", "NAME_FIRST"), ("email", "EMAIL_ADDRESS"),
                ("telefono", "PHONE_HOME_WHOLE_NUMBER"), ("direccion.via", "ADDRESS_HOME_STREET_NAME"),
                ("direccion.numero", "ADDRESS_HOME_HOUSE_NUMBER"),
                ("direccion.cp", "ADDRESS_HOME_ZIP"), ("direccion.municipio", "ADDRESS_HOME_CITY"),
                ("direccion.provincia", "ADDRESS_HOME_STATE"),
                ("direccion.pais", "ADDRESS_HOME_COUNTRY"), ("direccion.piso", "ADDRESS_HOME_FLOOR"))


def autofill_address(profile: dict | None = None) -> list[dict]:
    """El payload de `Autofill.trigger` a partir del perfil, con los nombres filtrados por la whitelist.

    `NAME_LAST` lleva los dos apellidos juntos porque es lo que el clasificador rellenó: las etiquetas
    «Primer apellido» y «Segundo apellido» no las reconoce, y esos dos campos son trabajo del bucle
    determinista.
    """
    data = _profile if profile is None else profile
    out = [{"name": name, "value": data[key]} for key, name in AUTOFILL_MAP if data.get(key)]
    surname = " ".join(x for x in (data.get("apellido1"), data.get("apellido2")) if x)
    if surname:
        out.append({"name": "NAME_LAST", "value": surname})
    bad = sorted({f["name"] for f in out} - AUTOFILL_SAFE)
    if bad:
        raise FormError(f"nombres de tipo fuera de la whitelist medida: {bad}. 10 de los 144 nombres "
                        "matan el proceso del navegador, así que lo que no esté en la lista no se manda.")
    return out


# --- la cosecha de formulario ------------------------------------------------------------------------
# NO es una segunda cosecha: `observe.HARVEST` ya trae type, pattern, maxlength, min, max, step,
# inputmode, autocomplete, los valores de las `<option>` y la validez real, y deja los nodos en
# `globalThis.__MANOS_NODES`. Esto se monta encima: indexa esos nodos y añade lo que un formulario
# necesita y `observe` no da —la cadena de etiquetado CON SU PROCEDENCIA, la sección del fieldset, los
# radios agrupados en UN campo con opciones, el estado ARIA del combobox, el botón que abre un input de
# ficheros y los botones de envío con su clasificación— más **los campos que la cosecha salta por
# invisibles**, que son justo los que un formulario necesita ver: un `input[type=file]` escondido detrás
# de un botón bonito, o un paso de asistente que todavía no se ha revelado. Los nodos nuevos se empujan
# al mismo array, así que todo el módulo direcciona por un solo índice.

# Lo que hace irreversible un botón de envío aunque se llame «Siguiente» o «Guardar». JS y Python leen la misma
# expresión; server.py la usa también para no repetir un clic así (`_commit`).
PAY = r"\bpagar\b|\bpago\b|\bcomprar\b|confirmar pedido|aceptar y firmar|\bcheckout\b"
# Lo que compromete dicho por el nombre de un botón suelto (sin <form>, que la clasificación de arriba ve como
# `button`): «Confirmar envío». Lo usa replay.py para no repetir un clic así.
SEND = r"\b(pay|buy|purchase|order|pedido|delete|borrar|eliminar|confirm|confirmar|enviar|submit|send|firmar|sign)"

ADDENDUM = r"""(cfg => {
  const N = (globalThis.__MANOS_NODES = globalThis.__MANOS_NODES || []);
  const at = new Map(N.map((el, i) => [el, i]));
  const SEL = 'input,select,textarea,[contenteditable=""],[contenteditable=true],' +
              '[role=checkbox],[role=radio],[role=switch],[role=combobox]';
  const BTN = 'button,input[type=submit],input[type=image],input[type=reset],input[type=button]';
  const DEAD = new Set(['hidden', 'submit', 'reset', 'button', 'image']);
  // The same rule as observe.HARVEST: a structural type may emit its value because it cannot be a
  // secret; everything else is free text, and of free text only the length comes out.
  const STRUCT = new Set(['checkbox','radio','button','submit','reset','image','file','range','color',
    'date','time','datetime-local','month','week']);
  const DRAFT = /guardar|borrador|\bsave\b|save draft|save for later|continuar m[aá]s tarde/i;
  const STEP = /\bsiguiente\b|\bnext\b|\bcontinuar\b|\bcontinue\b/i;
  const PAY = /""" + PAY + r"""/i;

  const txt = el => el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const root = el => el.getRootNode();
  const byId = (el, id) => {
    const r = root(el);
    return (r && r.getElementById) ? r.getElementById(id) : document.getElementById(id);
  };
  const refs = (el, attr) => {
    const v = el.getAttribute && el.getAttribute(attr);
    if (!v) return '';
    const out = [];
    for (const id of v.split(/\s+/)) { const t = byId(el, id); if (t) out.push(txt(t)); }
    return out.join(' ').trim();
  };
  const vis = el => {
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.01
           && r.width > 2 && r.height > 2;
  };
  // `closest` se para en el borde del shadow root, así que un campo dentro de un componente se queda
  // sin su `fieldset` y sin su `legend` — y eso es justo donde vive la mitad de los formularios de hoy.
  // Se sube cruzando por el host, que es la jerarquía que ve una persona mirando la página.
  const up = (el, sel) => {
    for (let n = el; n; n = root(n).host) {
      const hit = n.closest && n.closest(sel);
      if (hit) return hit;
      if (!(root(n) instanceof ShadowRoot)) return null;
    }
    return null;
  };
  function* walk(node, sel) {
    for (const el of (node.querySelectorAll ? node.querySelectorAll(sel) : [])) yield el;
    for (const el of (node.querySelectorAll ? node.querySelectorAll('*') : []))
      if (el.shadowRoot) yield* walk(el.shadowRoot, sel);
  }

  // The HTML-AAM chain, in order, each rung naming itself, and the first two rungs in the SAME order
  // as `observe.nameOf` — one precedence for the whole repository, because `sanitize.classify` reads
  // this name. `nearby` is the rung to distrust: capped at 40 chars and stopped at the previous
  // control, and it is flagged so the model knows to look at the screenshot instead of trusting it.
  function labelOf(el) {
    const lb = refs(el, 'aria-labelledby');
    if (lb) return [lb, 'labelledby'];
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return [al.trim(), 'aria-label'];
    if (el.labels && el.labels.length) {
      const t = txt(el.labels[0]);
      if (t) return [t, el.labels[0].htmlFor ? 'label-for' : 'label-wrapped'];
    }
    if (el.id) {         // el.labels is empty for anything that is not a labelable element
      const r = root(el);
      const l = r.querySelector ? r.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
      if (l) { const t = txt(l); if (t) return [t, 'label-for']; }
    }
    if (el.placeholder) return [el.placeholder, 'placeholder'];
    const ti = el.getAttribute('title');
    if (ti) return [ti, 'title'];
    let acc = '';
    for (let n = el.previousSibling; n && acc.length < 40; n = n.previousSibling) {
      // Se para en el control anterior Y en cualquier cosa que lo contenga: un `<p>` con el campo de
      // arriba dentro no es «lo que hay cerca de este campo», es la etiqueta del otro.
      if (n.nodeType === 1 && n.matches
          && (n.matches(SEL + ',' + BTN) || n.querySelector(SEL + ',' + BTN))) break;
      acc = (((n.nodeType === 3 ? n.nodeValue : txt(n)) || '') + ' ' + acc).replace(/\s+/g, ' ').trim();
    }
    acc = acc.slice(-40).trim();
    if (acc) return [acc, 'nearby'];
    const fs = up(el, 'fieldset');
    const lg = fs && fs.querySelector('legend');
    return lg ? [txt(lg), 'legend'] : ['', ''];
  }

  function sectionOf(el) {
    const fs = up(el, 'fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return txt(lg); }
    const gr = up(el, '[role=group][aria-label]');
    if (gr) return gr.getAttribute('aria-label');
    for (let n = el; n; n = n.parentElement)
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling)
        if (/^H[1-6]$/.test(s.tagName)) return txt(s);
    return '';
  }

  function kindOf(el) {
    if (el.isContentEditable) return 'richtext';
    const role = (el.getAttribute('role') || '').split(/\s+/)[0];
    if (el.tagName === 'SELECT') return el.multiple ? 'multiselect' : 'select';
    if (el.tagName === 'TEXTAREA') return 'textarea';
    if (el.tagName === 'INPUT') return role === 'combobox' ? 'combobox' : el.type;
    return role || 'generic';
  }

  const freeValue = el => {
    if (el.isContentEditable) return txt(el);
    if (el.tagName === 'TEXTAREA') return el.value;
    if (el.tagName !== 'INPUT' || STRUCT.has(el.type)) return null;
    return el.value;
  };

  const forms = [], fmap = new Map();
  const formOf = el => {
    const f = el.form || (el.closest && el.closest('form'));
    if (!f) return -1;
    if (!fmap.has(f)) {
      fmap.set(f, forms.length);
      forms.push({id: f.id || '', action: f.getAttribute('action') || '',
                  method: (f.getAttribute('method') || 'get').toLowerCase(),
                  novalidate: f.noValidate ? 1 : 0, valid: f.checkValidity() ? 1 : 0});
    }
    return fmap.get(f);
  };

  const fields = [], groups = new Map(), seen = new Set();
  for (const el of walk(document, SEL)) {
    if (seen.has(el)) continue;
    seen.add(el);
    if (el.tagName === 'INPUT' && DEAD.has(el.type)) continue;
    const k = kindOf(el);
    if (k === 'generic') continue;
    const [label, from] = labelOf(el);
    const o = {i: at.has(el) ? at.get(el) : (N.push(el) - 1), k: k, label: label, from: from,
               form: formOf(el)};
    if (!vis(el)) o.hidden = 1;
    if (el.name) o.nm = el.name;
    const sec = sectionOf(el); if (sec) o.section = sec;
    if (el.disabled) o.dis = 1;
    if (el.readOnly) o.ro = 1;
    if (el.required || el.getAttribute('aria-required') === 'true') o.req = 1;
    if (el.multiple) o.multi = 1;
    if (root(el) !== document) o.shadow = 1;
    // Las señales del nivel B del saneado, ninguna emitida: el nombre accesible, la otra etiqueta
    // aria (la que no ganó la cadena), el id y el placeholder. `al` va aquí y en `observe.HARVEST`
    // con el mismo significado, así que las dos cosechas clasifican igual el mismo campo.
    o.n = label;
    const al = el.getAttribute('aria-label');
    if (al && al.trim() && al.trim() !== label) o.al = al.trim();
    if (el.id) o.id = el.id;
    if (el.placeholder) o.ph = el.placeholder;
    for (const a of ['pattern','minlength','maxlength','min','max','step','inputmode','accept']) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v !== null && v !== undefined && v !== '') o[a] = v;
    }
    const ac = el.getAttribute('autocomplete'); if (ac) o.ac = ac;

    if (k === 'checkbox' || k === 'radio' || k === 'switch') {
      o.chk = (el.tagName === 'INPUT' ? el.checked
               : el.getAttribute('aria-checked') === 'true') ? 1 : 0;
      if (el.indeterminate) o.indet = 1;
    } else if (k === 'file') {
      o.files = [...el.files].map(f => [f.name, f.size]);
    } else {
      const free = freeValue(el);
      if (free !== null) { o.vl = free.length; if (free) o.fv = free.slice(0, 8192); }
      else if (el.value) o.v = String(el.value).slice(0, 120);
    }
    if (el.tagName === 'SELECT') {
      let opts = [...el.options].map(op => [op.value, (op.label || '').trim().slice(0, 40)]);
      o.opts_n = opts.length;
      if (cfg.prefix) {
        const q = cfg.prefix.toLowerCase();
        opts = opts.filter(p => p[1].toLowerCase().startsWith(q) || p[0].toLowerCase().startsWith(q));
      }
      o.opts = opts.slice(0, cfg.max_opts);
      if (el.multiple) o.sel_t = [...el.selectedOptions].map(op => (op.label || '').trim()).join(', ');
      else if (el.selectedIndex >= 0) o.sel_t = (el.options[el.selectedIndex].label || '').trim();
    }
    if (el.willValidate) {
      const ok = el.checkValidity();
      o.valid = ok ? 1 : 0;
      if (!ok) {
        const why = [];
        for (const key in el.validity) if (key !== 'valid' && el.validity[key]) why.push(key);
        o.why = why.join(','); o.msg = el.validationMessage;
      }
    }
    try { if (el.matches(':user-invalid')) o.ue = 1; } catch (e) {}
    // El `aria-describedby` NO se lee aquí: es el `desc` de `observe.HARVEST`, el mismo dato bajo otra
    // clave, y el merge ya lo trae. Un campo oculto se queda sin él, que es el precio correcto: no hay
    // mensaje de error que leer en un campo que todavía no se ha revelado.
    const hp = el.getAttribute('aria-haspopup'); if (hp) o.haspopup = hp;
    const ct = el.getAttribute('aria-controls'); if (ct) o.controls = ct;
    const ex = el.getAttribute('aria-expanded'); if (ex !== null) o.expanded = ex;

    // A radio group is ONE field with options, not N booleans: one field per radio invites the model to
    // set two of them to true. `ix` carries each option's own node index, because filling the group
    // means focusing the member that holds the wanted value, not the first one.
    if (k === 'radio' && el.name) {
      const key = o.form + '#' + el.name;
      const g = groups.get(key);
      if (g) {
        g.opts.push([el.value, label]);
        g.ix.push(o.i);
        if (o.chk) { g.v = el.value; g.sel_t = label; }
        if (o.req) g.req = 1;
        continue;
      }
      o.opts = [[el.value, label]];
      o.ix = [o.i];
      o.group = el.name;
      const gr = el.closest && el.closest('[role=group][aria-label]');
      o.label = gr ? gr.getAttribute('aria-label') : el.name;
      o.from = gr ? 'group' : 'name';
      if (o.chk) { o.v = el.value; o.sel_t = label; }
      delete o.chk;
      groups.set(key, o);
    }
    fields.push(o);
  }
  for (const g of groups.values()) g.opts_n = g.opts.length;

  // The styled-button + hidden-input pattern: the field says which button opens it.
  for (const f of fields) {
    if (f.k !== 'file' || !f.hidden) continue;
    const p = N[f.i].parentElement;
    const b = p && (p.querySelector(BTN) || (p.parentElement && p.parentElement.querySelector(BTN)));
    if (b) { f.opened_by = txt(b) || b.value || ''; b.dataset.manosOpens = String(f.i); }
  }

  // El texto que rodea a un botón, SIN el de los otros botones: cuatro en el mismo `<p>` se
  // contaminaban entre ellos y un «Confirmar pedido» convertía en irreversible al `Siguiente` de al
  // lado, que es justo la clasificación que decide si hace falta el visto bueno del usuario.
  const around = el => {
    const p = el.parentElement;
    if (!p) return '';
    let out = '';
    for (const n of p.childNodes) {
      if (n.nodeType === 3) out += ' ' + n.nodeValue;
      else if (n !== el && n.matches && !n.matches(BTN)) out += ' ' + txt(n);
    }
    return out.replace(/\s+/g, ' ').trim().slice(0, 200);
  };

  const beforeunload = window.onbeforeunload != null;
  const buttons = [];
  for (const el of walk(document, BTN)) {
    const type = (el.getAttribute('type') || (el.tagName === 'BUTTON' ? 'submit' : 'button'))
                 .toLowerCase();
    const b = {i: at.has(el) ? at.get(el) : (N.push(el) - 1), form: formOf(el),
               t: (el.tagName === 'INPUT' ? el.value : txt(el)).slice(0, 80),
               around: around(el)};
    if (type === 'submit' || type === 'image') b.submit = 1;
    const fa = el.getAttribute('formaction'); if (fa) b.formaction = fa;
    if (el.dataset.manosOpens !== undefined) {
      b.opens = +el.dataset.manosOpens;
      delete el.dataset.manosOpens;
    }
    if (!vis(el)) b.hidden = 1;
    buttons.push(b);
  }
  // Irreversible by default, always. `draft` only when the name matches, it is not the only submit and
  // it does not carry a formaction elsewhere; `step` only when there really are hidden fields behind.
  const nsub = buttons.filter(b => b.submit).length;
  const later = fields.some(f => f.hidden && f.req);
  for (const b of buttons) {
    if (!b.submit) { b.cls = b.opens !== undefined ? 'opens_file' : 'button'; delete b.around; continue; }
    const form = b.form >= 0 ? forms[b.form] : null;
    let cross = false;
    try {
      cross = new URL(b.formaction || (form ? form.action : '') || location.href, location.href)
                .origin !== location.origin;
    } catch (e) {}
    if (PAY.test(b.t + ' ' + b.around) || cross || beforeunload) b.cls = 'irreversible';
    else if (DRAFT.test(b.t) && nsub > 1 && !b.formaction) b.cls = 'draft';
    else if (STEP.test(b.t) && later) b.cls = 'step';
    else b.cls = 'irreversible';
    delete b.around;
  }

  return {url: location.href, lang: document.documentElement.lang || '',
          forms: forms, fields: fields, buttons: buttons};
})"""


# --- un JS por cosa ----------------------------------------------------------------------------------
# Todos direccionan el campo por su índice en `__MANOS_NODES`, y todos contestan `gone` o `detached` en
# vez de lanzar: un elemento desprendido no es un fallo, es un `change` que repintó el formulario, que es
# el caso normal, y entonces lo que toca es volver a describir.

_HEAD = ("(a => { const el = (globalThis.__MANOS_NODES || [])[a.i];"
         " if (!el) return {gone: 1}; if (!el.isConnected) return {detached: 1};")

PREP = _HEAD + r"""
  try { el.scrollIntoView({block: 'center', behavior: 'instant'}); } catch (e) {}
  if (el.focus) el.focus({preventScroll: true});
  // The focus assertion MUST descend: for an element inside a shadow root document.activeElement is
  // the HOST, so the naive check is false exactly where it matters.
  let act = document.activeElement;
  while (act && act.shadowRoot && act.shadowRoot.activeElement) act = act.shadowRoot.activeElement;
  if (a.clear) {
    if (el.isContentEditable) getSelection().selectAllChildren(el);
    else if (el.select) el.select();
  }
  const r = el.getBoundingClientRect();
  const hr = el.getRootNode();
  const hit = (hr.elementFromPoint ? hr : document)
                .elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  return {focused: (act === el || (el.contains && el.contains(act))) ? 1 : 0,
          fk: act ? (act.tagName === 'INPUT' ? act.type : act.tagName.toLowerCase()) : '',
          fac: (act && act.getAttribute && act.getAttribute('autocomplete')) || '',
          rect: [r.x, r.y, r.width, r.height],
          nm: el.name || '', eid: el.id || '', tag: el.tagName,
          hit: (hit === el || (el.contains && el.contains(hit))) ? 1 : 0,
          covered: (hit && hit !== el && !el.contains(hit))
                   ? (hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '')) : ''};
})"""

# The native prototype setter, picked off the RIGHT prototype or it throws Illegal invocation. This is
# the only sanctioned assignment path: it bypasses React's instance-level value tracker, which is why a
# plain `el.value = x` leaves the component state empty and poisons the next real click.
SET_NATIVE = _HEAD + r"""
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
              : HTMLInputElement.prototype;
  const d = Object.getOwnPropertyDescriptor(proto, 'value');
  if (!d || !d.set) return {err: 'el elemento no tiene setter nativo de value'};
  try { d.set.call(el, a.v); } catch (e) { return {err: String(e)}; }
  el.dispatchEvent(new InputEvent('input',
    {bubbles: true, composed: true, inputType: 'insertText', data: a.v}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: 1};
})"""

# What Playwright does, exactly: never `select.value = x` alone, because the value changes and NOTHING
# is notified. Matching is value, then label, then prefix, then index.
SELECT_OPTION = _HEAD + r"""
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const opts = [...el.options];
  const pick = w => opts.find(o => o.value === String(w))
                 || opts.find(o => norm(o.label) === norm(w))
                 || (norm(w) ? opts.find(o => norm(o.label).startsWith(norm(w))) : null)
                 || (/^\d+$/.test(w) ? opts[+w] : null);
  const want = Array.isArray(a.v) ? a.v : [a.v];
  const hits = want.map(pick).filter(Boolean);
  if (hits.length !== want.length)
    return {err: 'no_option', opts_n: opts.length,
            sample: opts.slice(0, 12).map(o => [o.value, (o.label || '').trim()])};
  el.value = undefined;
  for (const h of hits) h.selected = true;
  el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: 1, t: hits.map(h => (h.label || '').trim()).join(', ')};
})"""

# For a visually-replaced input (opacity:0, 0x0, under a styled span) a click at the input's own rect
# lands nowhere; the label's activation behaviour forwards to the control.
CLICK_LABEL = _HEAD + r"""
  const l = (el.labels && el.labels[0]) || (el.closest && el.closest('label'));
  if (!l) return {err: 'el campo no tiene etiqueta que pulsar'};
  l.click();
  return {ok: 1};
})"""

READBACK = _HEAD + r"""
  const o = {};
  const role = (el.getAttribute('role') || '').split(/\s+/)[0];
  if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio'))
    o.chk = el.checked ? 1 : 0;
  else if (/^(checkbox|radio|switch)$/.test(role))
    o.chk = el.getAttribute('aria-checked') === 'true' ? 1 : 0;
  else if (el.tagName === 'SELECT') {
    o.v = el.value;
    o.t = el.multiple ? [...el.selectedOptions].map(x => (x.label || '').trim()).join(', ')
        : (el.selectedIndex >= 0 ? (el.options[el.selectedIndex].label || '').trim() : '');
  }
  else if (el.type === 'file') o.files = [...el.files].map(f => [f.name, f.size]);
  else if (el.isContentEditable) o.v = (el.innerText || '').replace(/ /g, ' ');
  else o.v = el.value;
  if (el.willValidate) {
    o.valid = el.checkValidity() ? 1 : 0;
    if (!o.valid) {
      const w = [];
      for (const k in el.validity) if (k !== 'valid' && el.validity[k]) w.push(k);
      o.why = w.join(','); o.msg = el.validationMessage;
    }
  }
  try { if (el.matches(':user-invalid')) o.ue = 1; } catch (e) {}
  return o;
})"""

# A combobox is verified by the input's value, never by the listbox closing: a closed listbox with an
# unchanged input is a silent failure.
COMBO = _HEAD + r"""
  const id = el.getAttribute('aria-controls');
  const r = el.getRootNode();
  const box = id ? ((r.getElementById ? r.getElementById(id) : null)
                    || document.getElementById(id)) : null;
  return {expanded: el.getAttribute('aria-expanded'),
          active: el.getAttribute('aria-activedescendant') || '',
          v: el.value,
          opts: box ? [...box.querySelectorAll('[role=option]')]
                       .map(o => [o.id, (o.textContent || '').replace(/\s+/g, ' ').trim()]) : []};
})"""

# The pre-submit gate, free and mandatory: form.checkValidity() plus [field, why, message] for every
# element that fails. It is what caught a required field inside a hidden wizard step. A control inside a
# shadow root is NOT in form.elements, so it is swept separately or it would never block anything.
GATE = _HEAD + r"""
  const f = el.form || (el.closest && el.closest('form'));
  const name = c => c.getAttribute('aria-label')
                 || (c.labels && c.labels[0] ? (c.labels[0].textContent || '').replace(/\s+/g,' ').trim() : '')
                 || c.name || c.id;
  const bad = [];
  const check = c => {
    if (!c.willValidate || c.checkValidity()) return;
    const w = [];
    for (const k in c.validity) if (k !== 'valid' && c.validity[k]) w.push(k);
    bad.push([name(c), w.join(','), c.validationMessage]);
  };
  if (f) for (const c of f.elements) check(c);
  const owned = new Set(f ? [...f.elements] : []);
  (function sweep(node) {
    for (const c of node.querySelectorAll('input,select,textarea')) if (!owned.has(c)) check(c);
    for (const c of node.querySelectorAll('*')) if (c.shadowRoot) sweep(c.shadowRoot);
  })(document);
  return {valid: ((f ? f.checkValidity() : true) && !bad.length) ? 1 : 0, bad: bad,
          t: (el.tagName === 'INPUT' ? el.value : (el.textContent || '').replace(/\s+/g, ' ').trim()),
          action: f ? (f.getAttribute('action') || location.href) : '',
          method: f ? (f.getAttribute('method') || 'get').toLowerCase() : 'get'};
})"""

# The fingerprint the token binds to: the form's own serialised state. The values cross into the server
# —which already knows them, it wrote them— and only the digest is kept, so nothing echoes to the model.
# ponytail: known ceiling — FormData covers form-owned controls. A contenteditable or a field inside a
# shadow root changing between the readback and the submit is not caught; the fix, if it ever matters,
# is to walk the described fields instead, at one round trip per field.
FINGERPRINT = _HEAD + r"""
  const f = el.form || (el.closest && el.closest('form'));
  if (!f) return {fp: ''};
  return {fp: [...new FormData(f).entries()]
               .map(e => e[0] + '=' + (typeof e[1] === 'string' ? e[1] : '(file:' + e[1].name + ')'))
               .join(String.fromCharCode(1))};
})"""

# El `change` de un campo de texto NO salta al teclear: salta al perder el foco. Y la revelación
# progresiva de medio internet cuelga de ese `change` —el paso 2 que aparece cuando el correo valida—,
# así que después de escribir se suelta el foco, que es exactamente lo que hace una persona al pasar al
# campo siguiente. El tecleo es una edición de usuario de confianza, así que el `change` que sale de
# aquí es el del navegador y no uno sintético.
BLUR = _HEAD + " if (el.blur) el.blur(); return {ok: 1}; })"

# El diff del Autofill necesita VALORES, y el estrangulamiento se los quita a la cosecha siempre y con
# razón (`sanitize.classify_rows` hace `pop("fv")` sin excepciones). Así que esto los lee aparte. No es
# una segunda cosecha —ni etiquetas, ni restricciones, ni oclusión, solo nombre y valor— y los valores
# no salen de `_autofill`: lo único que se devuelve son los NOMBRES de los campos que cambiaron.
VALUES = """(() => {
  const o = {};
  for (const el of document.querySelectorAll('input[name],select[name],textarea[name]')) {
    if (el.type === 'radio' || el.type === 'checkbox') { if (!el.checked) continue; }
    o[el.name] = String(el.value);
  }
  return o;
})()"""

NODE_GONE = ("(a => { const el = (globalThis.__MANOS_NODES || [])[a.i];"
             " return {gone: (!el || !el.isConnected) ? 1 : 0}; })")

# Evidence five: the confirmation identifier, which is what the user actually needs afterwards, plus any
# live region that appeared, which is evidence four.
CONFIRM_JS = r"""(() => {
  const body = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  const near = body.match(
    /(?:confirmaci[oó]n|confirmation|referencia|reference|localizador|n[uú]mero de (?:solicitud|pedido|expediente|reserva))[^A-Za-z0-9]{0,20}([A-Z0-9][A-Z0-9-]{5,})/i);
  return {confirmation: near ? near[1] : '',
          live: [...document.querySelectorAll('[role=status],[role=alert],[aria-live]')]
                  .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 3)};
})()"""


# --- la tabla de ids de campo ------------------------------------------------------------------------
# `form_describe` es la única que acuña ids de campo, igual que `browser_observe` es la única que acuña
# refs: dos espacios de nombres distintos a propósito. Un id viejo devuelve `StaleHandle`, nunca un
# relleno equivocado.

@dataclass
class Field:
    fid: int
    frame_id: str
    row: dict
    sent: str = ""      # la clave `{{perfil.*}}` con la que lo rellenó el SERVIDOR, si fue el caso:
                        # de un campo así no sale el valor en ninguna tanda, ni en la que lo escribió
                        # ni en las siguientes, porque el modelo mandó una clave y no un dato

    @property
    def i(self) -> int:
        return self.row["i"]


_fields: dict[int, Field] = {}
_buttons: list[dict] = []
_url = ""
_tokens: dict[str, dict] = {}
_submitted: dict[tuple[str, str], float] = {}


def reset() -> None:
    """Olvida los ids, los botones y los tokens: al navegar o al cerrar, nada de esto sigue siendo
    cierto, y un token vivo sobre una página que ya no está es un envío equivocado.

    **`_submitted` no se toca aquí.** Su clave es `(acción, huella)` y no caduca con la página, así que
    borrarla al navegar —y `submit()` llama a `reset()` justo después de enviar— era borrar el freno
    del doble envío en el único momento en el que tenía algo que frenar.
    """
    _fields.clear()
    _buttons.clear()
    _tokens.clear()


def forget_submits() -> None:
    """Olvida los envíos recientes. Solo al cerrar el navegador: abrir otro es una intención nueva."""
    _submitted.clear()


def _get(fid) -> Field:
    try:
        return _fields[int(fid)]
    except (KeyError, TypeError, ValueError):
        raise StaleHandle(f"el campo {fid!r} no existe en la última descripción. Los ids los acuña "
                        "form_describe y mueren con ella. Vuelve a llamar a form_describe.") from None


# --- las sesiones CDP por frame ----------------------------------------------------------------------

class Frames:
    """Las sesiones CDP por frame, vivas durante una llamada entera.

    `Page.getFrameTree` es **por proceso**, así que un OOPIF solo se direcciona desde su propia sesión;
    `observe._frame_sessions` ya hace ese baile. Aquí se mantienen abiertas mientras se rellena, porque
    el bucle vuelve a cada frame una vez por campo.
    """

    def __init__(self, session):
        self.session = session
        self.cdp: dict[str, object] = {}
        self.order: list[tuple[str, str]] = []
        self.main = ""        # el frame principal, que es el primero que devuelve _frame_sessions
        self._owned: list = []

    async def __aenter__(self):
        for cdp, tree, own in await observe._frame_sessions(self.session):
            if own:
                self._owned.append(cdp)
            for frame in observe._flatten(tree):
                self.cdp[frame["id"]] = cdp
                self.main = self.main or frame["id"]
                self.order.append((frame["id"], urlsplit(frame.get("url", "")).netloc))
        return self

    async def __aexit__(self, *_exc):
        for cdp in self._owned:
            try:
                await cdp.detach()
            except Exception:
                pass

    def of(self, frame_id: str):
        cdp = self.cdp.get(frame_id)
        if cdp is None:
            raise StaleHandle("el frame del campo ya no está en la página. Vuelve a llamar a "
                            "form_describe.")
        return cdp


async def _eval1(cdp, frame_id: str, code: str, args: dict | None = None):
    return await observe._eval(cdp, frame_id,
                               code if args is None else f"{code}({json.dumps(args)})")


async def _object_id(cdp, frame_id: str, i: int) -> str:
    """El `objectId` del nodo, que es lo que `DOM.describeNode` necesita para dar un `backendNodeId`."""
    await observe._eval(cdp, frame_id, "1")        # makes sure the isolated world exists
    got = await cdp.send("Runtime.evaluate", {
        "expression": f"(globalThis.__MANOS_NODES||[])[{int(i)}]",
        "contextId": observe._worlds[frame_id], "returnByValue": False})
    object_id = got["result"].get("objectId")
    if not object_id:
        raise StaleHandle("el campo ya no está en la página. Vuelve a llamar a form_describe.")
    return object_id


# --- describir ---------------------------------------------------------------------------------------

async def _harvest(frames: Frames, viewport_only: bool = False, prefix: str = "",
                   base: bool = False) -> list[tuple]:
    """`[(frame_id, host, payload)]`, una entrada por frame: el añadido, y en la primera pasada también
    la cosecha de `observe`, que es la que crea `globalThis.__MANOS_NODES`.

    `base` solo en `form_describe`, y la razón es exacta: `observe.HARVEST` **reinicia** ese array en
    cada pasada, y todo este módulo direcciona los campos por su índice en él. El añadido, en cambio, lo
    respeta y solo añade al final, así que un id acuñado sigue apuntando al mismo nodo mientras el
    formulario se rellena, incluso si un paso del asistente revela campos nuevos.
    """
    out = []
    for frame_id, host in frames.order:
        cdp = frames.cdp[frame_id]
        rows = {}
        if base:
            first = await _eval1(cdp, frame_id, observe.HARVEST, {"viewport_only": viewport_only})
            rows = {r["i"]: r for r in first["els"]}
            sanitize.remember(first["vals"])        # the type=hidden values, which emit no row
        extra = await _eval1(cdp, frame_id, ADDENDUM, {"prefix": prefix, "max_opts": MAX_OPTS})
        for row in extra["fields"]:
            # One merge with one precedence: the form view wins where it speaks, and the harvest's own
            # keys —`xp`, `desc`, the occlusion facts— come through untouched where it does not.
            #
            # Y el nombre ya no es de los contestados: las dos cosechas calculan el nombre accesible
            # con la MISMA precedencia (HTML-AAM: `aria-labelledby` antes de `aria-label`) y emiten la
            # otra etiqueta aria en `al`, así que la clasificación del nivel B es la misma la mire quien
            # la mire. Es lo que hay que mantener cierto; tener dos cadenas con precedencias opuestas
            # era lo que daba dos niveles de sensibilidad para un solo campo.
            #
            # La emisión del VALOR, la validez y los atributos sí siguen duplicadas aquí, y es
            # deliberado: `fill()` cosecha SIN la base —`observe.HARVEST` reinicia `__MANOS_NODES` y
            # eso invalidaría todos los ids a media escritura— y la base salta los campos invisibles,
            # que son justo los que una puerta previa al envío necesita. Son las dos razones por las
            # que no se puede borrar; `tests/test_forms.py` asierta que las dos dicen lo mismo.
            row.update({k: v for k, v in rows.get(row["i"], {}).items() if k not in row})
        # Pasarela de pago = un IFRAME, y por eso la detección no se aplica al frame principal: una
        # página de checkout normal tiene su `cc-number` en el documento de arriba, y marcar el frame
        # entero de nivel A dejaría al agente sin rellenar ni la dirección de envío. El número y el CVC
        # siguen protegidos por sí mismos, que es lo que `sanitize.HARD_AUTOCOMPLETE` ya hace.
        extra["pay"] = frame_id != frames.main and (
            bool(PAY_HOST.search(host))
            or any(str(f.get("ac", "")).startswith("cc-") for f in extra["fields"]))
        out.append((frame_id, host, extra))
    return _classify(out)


def _classify(frames: list[tuple]) -> list[tuple]:
    """Marca los campos sensibles y les quita el valor, con la regla de `sanitize` y no con una segunda.

    Deja además puestos los índices que la redacción de la captura necesita: se tapa TODO campo de texto
    libre, no solo los que el clasificador marcó, porque una imagen es contexto igual que el texto.
    """
    for frame_id, _host, data in frames:
        data["secrets"] = sanitize.classify_rows(data["fields"])
        if data["pay"]:
            for row in data["fields"]:
                row["sec"], row["pay"] = row.get("sec") or "A", 1
        observe._secrets[frame_id] = [r["i"] for r in data["fields"] if r.get("sec") or "vl" in r]
    return frames


def _render(row: dict) -> str | None:
    """El valor tal como lo leería una persona: la fecha como 25/12/1990 y un select por el TEXTO de su
    opción, no por su valor, porque un `["prov","MU"]` no se puede confirmar.

    `None` significa «este campo no se puede mostrar»: es de nivel A o B y de él solo sale la longitud.
    """
    if row.get("sec"):
        return None
    if "chk" in row:
        return "sí" if row["chk"] else "no"
    if row.get("k") == "file":
        return ", ".join(n for n, _s in row.get("files", ()))
    if row.get("sel_t") is not None:
        return row["sel_t"]
    if row.get("k") == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("v", ""))):
        return datetime.strptime(row["v"], "%Y-%m-%d").strftime("%d/%m/%Y")
    return str(row.get("v", ""))


def _public(row: dict, host: str) -> dict:
    """La fila que ve el modelo: claves cortas porque se repiten, y nunca el valor de un texto libre."""
    out = {"i": row["fid"], "k": row["k"], "label": row.get("label", ""), "from": row.get("from", "")}
    for key in ("section", "nm", "req", "dis", "ro", "hidden", "shadow", "pattern", "minlength",
                "maxlength", "min", "max", "step", "inputmode", "accept", "multi", "group", "opts_n",
                "valid", "why", "msg", "ue", "haspopup", "controls", "expanded", "opened_by",
                "indet", "pay"):
        if row.get(key) not in (None, ""):
            out[key] = row[key]
    if row.get("desc"):
        out["error"] = row["desc"]      # el `aria-describedby` de la cosecha base, que es el mensaje
                                        # de error visible del propio sitio
    if row.get("ac"):
        out["autocomplete"] = row["ac"]
    if row.get("opts"):
        out["options"] = [{"v": v, "t": t} for v, t in row["opts"]]
    if row["k"] in ISO_FORMAT:
        out["format"] = ISO_FORMAT[row["k"]]
    elif row["k"] in ("text", "tel") and LOCAL_DATE.search(
            f"{row.get('pattern', '')} {row.get('ph', '')}"):
        out["format"] = "dd/mm/aaaa"
    if row.get("sec"):
        out["sec"], out["len"] = row["sec"], row.get("vl", len(str(row.get("v", ""))))
    elif "vl" in row:
        out["len"] = row["vl"]
    elif "chk" in row:
        out["checked"] = row["chk"]
    else:
        out["value"] = _render(row)
    if host:
        out["in"] = f"frame:{host}"
    return out


async def describe(session, scope: str = "page", include_hidden: bool = True,
                   option_prefix: str = "") -> dict:
    """La descripción del formulario, y la única tool que acuña ids de campo.

    `scope` es `page` (todos los frames), `viewport` (solo lo que está en pantalla) o `frame:<host>`.
    Los campos ocultos salen marcados `hidden` y **no se filtran**: dicen que el formulario tiene más
    detrás y son la prueba de la puerta previa al envío.
    """
    session.require_no_dialog()
    global _url
    async with Frames(session) as frames:
        harvested = await _harvest(frames, viewport_only=(scope == "viewport"),
                                   prefix=option_prefix, base=True)
    if scope.startswith("frame:"):
        want = scope.split(":", 1)[1]
        harvested = [f for f in harvested if f[1] == want]

    _url = session.page.url
    _fields.clear()
    _buttons.clear()
    fields, forms = [], []
    for frame_id, host, data in harvested:
        base = len(forms)
        for form in data["forms"]:
            forms.append({"i": len(forms), **form} | ({"in": f"frame:{host}"} if host else {}))
        for row in data["fields"]:
            if row.get("hidden") and not include_hidden:
                continue
            row["fid"] = len(_fields)
            if row.get("form", -1) >= 0:
                row["form"] += base
            _fields[row["fid"]] = Field(row["fid"], frame_id, row)
            fields.append(_public(row, host))
        for btn in data["buttons"]:
            opens = [r["fid"] for r in data["fields"] if r["i"] == btn.get("opens", -1)]
            _buttons.append({"t": btn["t"], "cls": btn["cls"], "submit": btn.get("submit", 0),
                             "hidden": btn.get("hidden", 0),
                             "form": btn["form"] + base if btn.get("form", -1) >= 0 else -1,
                             "for": opens[0] if opens else None,
                             "i": btn["i"], "frame": frame_id})
    return sanitize.scrub({
        "url": sanitize.clean_url(_url),
        "lang": harvested[0][2]["lang"] if harvested else "",
        "forms": forms,
        "fields": fields,
        "buttons": [{k: v for k, v in b.items()
                     if k not in ("i", "frame", "action", "method")
                     and not (k in ("submit", "hidden") and not v)
                     and not (k in ("for", "form") and v in (None, -1))}
                    for b in _buttons],
        "secrets": sum(d["secrets"] for _f, _h, d in harvested),
        "next": "los ids de `fields` solo valen hasta la próxima form_describe. Rellena con "
                "form_fill(fields=[{\"id\": 0, \"value\": \"…\"}]); los datos del usuario se "
                "referencian como \"{{perfil.clave}}\" y los resuelve el servidor. `from` dice de "
                "dónde sale la etiqueta, y `from=\"nearby\"` es de CONFIANZA BAJA: es el texto que "
                "había al lado, no una etiqueta asociada, así que mira la captura antes de fiarte.",
    })


# --- rellenar ----------------------------------------------------------------------------------------

def _norm(value, row: dict) -> str:
    """La comparación es normalizada, porque una máscara nunca devuelve lo que escribiste y eso no es un
    fallo: `600112233` -> `600 11 22 33` y `123456` -> `1234,56 €` comparan iguales."""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    label = f"{row.get('label', '')} {row.get('nm', '')} {row.get('ph', '')}".lower()
    if row.get("k") == "tel" or re.search(r"tel[eé]fono|m[oó]vil", label):
        return re.sub(r"^(0034|34)", "", re.sub(r"\D", "", text))
    if re.search(r"iban|\bdni\b|\bnie\b|\bnif\b|matr[ií]cula|documento", label):
        return re.sub(r"[\s-]", "", text).upper()
    if row.get("k") in ("number", "range") or re.search(r"importe|precio|cantidad|€", label + text):
        return re.sub(r"\D", "", text)
    return re.sub(r"\s+", " ", text).casefold()


def _wanted(row: dict, value):
    """El valor que de verdad se escribe. Las dos derivaciones que el perfil promete, y las dos tienen
    que vivir aquí porque `_format_checks`, el tecleo y `_verdict` pasan los tres por esta función: una
    regla, no tres.

    1. Una fecha ISO en un campo de texto que pide dd/mm/aaaa se convierte, porque el locale es es_ES y
       la conversión es trabajo del servidor y no del modelo.
    2. El `+34` de un teléfono se cae salvo que el campo pida de verdad el formato internacional. El
       perfil lo guarda con prefijo porque es lo correcto para el Autofill, pero un campo español de 9
       dígitos con máscara se come el prefijo y deja el número truncado —medido en el fixture:
       `+34600112233` acaba en `346 00 11 22`— y entonces la relectura sale corta y el bucle lo lee como
       «la máscara rechaza la entrada programática», que es una parada. Se quita antes de teclear.

       Lo que decide si el campo es internacional es que el campo lo DIGA —un `+` en el `pattern`, en el
       `placeholder` o en la etiqueta—, no su `maxlength`. La prueba de que el largo no vale: el campo
       con máscara de `bench/fixtures/mask.html` tiene `maxlength=13` porque «600 11 22 33» ocupa 12
       caracteres, y con la regla del largo se le mandaba el prefijo y la máscara lo truncaba a
       «346 00 11 22», que es exactamente el fallo que esta derivación existe para evitar.
    """
    if not isinstance(value, str):
        return value
    if (row["k"] in ("text", "tel") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            and LOCAL_DATE.search(f"{row.get('pattern', '')} {row.get('ph', '')}")):
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    if INTL.match(value) and (row["k"] == "tel" or PHONE.search(
            f"{row.get('label', '')} {row.get('nm', '')} {row.get('ph', '')}")):
        wide = "+" in f"{row.get('pattern', '')} {row.get('ph', '')} {row.get('label', '')}"
        return value if wide else INTL.sub("", value)
    return value


def _truthy(value) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in (
        "true", "1", "sí", "si", "yes", "on")


def _format_checks(row: dict, value) -> str:
    """Las comprobaciones de formato de `dry_run`, sin tocar la página: es lo que pilla un formato de
    fecha equivocado antes de que llegue al sitio."""
    if row.get("dis"):
        return "el campo está deshabilitado"
    if row.get("ro"):
        return "el campo es de solo lectura"
    kind = row["k"]
    if row.get("group") or kind in SELECTISH:
        want = list(value) if isinstance(value, (list, tuple)) else [value]
        opts = row.get("opts", [])
        if row.get("opts_n", 0) > len(opts):
            return ""                      # the list came truncated; the page decides
        for one in want:
            text = str(one).strip().casefold()
            if not any(v == str(one) or t.strip().casefold() == text for v, t in opts):
                return (f"«{one}» no es una opción de este campo. Las que hay: "
                        + ", ".join(f"{v}={t}" for v, t in opts[:12]))
        return ""
    if kind in TOGGLE:
        return "" if isinstance(value, bool) or str(value).strip().lower() in (
            "true", "false", "sí", "si", "no", "1", "0", "on", "off") else \
            f"un {kind} solo acepta true o false, no {value!r}"
    if kind == "file":
        paths = [value] if isinstance(value, str) else list(value)
        if len(paths) > 1 and not row.get("multi"):
            return (f"{len(paths)} ficheros en un input sin `multiple`: el navegador se queda con el "
                    "primero EN SILENCIO, así que se rechaza aquí y no allí")
        accept = [a.strip().lower() for a in str(row.get("accept", "")).split(",") if a.strip()]
        for path in paths:
            try:
                p = Path(safe_file(path))
            except (PathRefused, ValueError) as exc:
                return str(exc)
            if accept and not any(p.name.lower().endswith(a) for a in accept if a.startswith(".")):
                return f"{p.name} no encaja con accept={row['accept']}"
        return ""
    text = str(value)
    if kind in ISO_FORMAT:
        shape = {"date": r"\d{4}-\d{2}-\d{2}", "time": r"\d{2}:\d{2}(:\d{2})?",
                 "month": r"\d{4}-\d{2}", "week": r"\d{4}-W\d{2}",
                 "datetime-local": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"}.get(kind)
        if shape and not re.fullmatch(shape, text):
            return (f"un input {kind} solo acepta el formato de cable {ISO_FORMAT[kind]} y «{text}» no "
                    "lo es. En estos campos no se puede teclear: tecla a tecla da basura.")
    if row.get("pattern") and text:
        try:
            # ponytail: known ceiling — `re` is not the JS RegExp engine. An HTML `pattern` is anchored
            # and almost always a character class, so the two agree; an exotic one falls through to the
            # browser's own validity, which is the authority anyway.
            if not re.fullmatch(row["pattern"], _wanted(row, text)):
                return f"«{_wanted(row, text)}» no cumple pattern={row['pattern']}"
        except re.error:
            pass
    if row.get("maxlength") and len(text) > int(row["maxlength"]):
        return f"{len(text)} caracteres para maxlength={row['maxlength']}"
    if row.get("minlength") and text and len(text) < int(row["minlength"]):
        return f"{len(text)} caracteres para minlength={row['minlength']}"
    for key, worse in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
        if not row.get(key):
            continue
        if kind in ("number", "range"):
            try:
                if worse(float(text), float(row[key])):
                    return f"{text} queda fuera de {key}={row[key]}"
            except ValueError:
                return f"«{text}» no es un número"
        elif kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) and worse(text, str(row[key])):
            return f"{text} queda fuera de {key}={row[key]}"
    return ""


def _verdict(row: dict, want, got: dict) -> str:
    """La tabla de decisión del bucle, entera. Tres cosas que parecen fallos y no lo son: una máscara que
    reformatea es un éxito, una relectura ilegible es un «vuelve a describir», y un valor que el
    navegador objeta es cosa del modelo y no del mecanismo."""
    if got.get("gone") or got.get("detached"):
        return "detached"
    if "chk" in got:
        return "ok" if bool(got["chk"]) == _truthy(want) else "retry"
    if row["k"] == "file":
        names = [n for n, _s in got.get("files", ())]
        paths = [want] if isinstance(want, str) else list(want)
        return "ok" if (len(names) == len(paths)
                        and all(Path(str(p)).name == n for p, n in zip(paths, names))) else "retry"
    pick = row["k"] in SELECTISH
    value = got.get("t", "") if pick else got.get("v", "")
    hit = (_norm(value, row) == _norm(want, row) or (pick and got.get("v") == str(want))
           or (not pick and _norm(value, row) == _norm(_wanted(row, want), row)))
    if hit:
        if got.get("valid") == 0 or got.get("ue"):
            return "needs_different_value"
        return "ok" if str(value) == str(_wanted(row, want)) else "mask"
    if got.get("valid") == 0 and got.get("why") in (
            "patternMismatch", "typeMismatch", "rangeUnderflow", "rangeOverflow", "stepMismatch",
            "tooLong", "badInput"):
        return "needs_different_value"
    return "retry"


def _shown(row: dict, got: dict, sent: str = "") -> dict:
    """Lo que de una relectura se puede enseñar. **La única excepción del proyecto a «el valor no sale»,
    con dos excepciones dentro:**

    - de un campo de nivel A o B salen la longitud y la validez, nunca el contenido, y lo que el modelo
      teclea en él se memoriza para que `scrub()` tache sus ecos en cualquier otra respuesta;
    - de un campo que el servidor rellenó desde `{{perfil.clave}}` salen la longitud, la validez y la
      clave. Esto **no** depende del clasificador, y es la diferencia que importa: la excepción de la
      relectura existe porque el modelo ya conoce el valor que mandó, y de un valor del perfil no lo
      conoce.
    """
    out = {k: got[k] for k in ("valid", "why", "msg", "ue") if got.get(k) is not None}
    if row.get("sec") or sent:
        value = str(got.get("t") or got.get("v") or "")
        if row.get("sec") and value:
            # Solo lo marcado. Lo que viene del perfil ya se memorizó en `resolve()`, y el TEXTO de una
            # opción es de la página y no del perfil: memorizarlo tacharía la página, que es la
            # regresión de la ronda anterior.
            sanitize.remember([value])
        out["len"] = len(value)
        if row.get("sec"):
            out["sec"] = row["sec"]
        if sent:
            out["sent"] = sent
        return out
    if "chk" in got:
        out["value"] = "sí" if got["chk"] else "no"
    elif "files" in got:
        out["value"] = ", ".join(f"{n} ({s} B)" for n, s in got["files"])
    else:
        raw = got.get("t") if got.get("t") else got.get("v", "")
        if row["k"] == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw)):
            raw = datetime.strptime(str(raw), "%Y-%m-%d").strftime("%d/%m/%Y")
        out["value"] = str(raw)
    return out


async def _click(cdp, frame_id: str, i: int) -> dict:
    """Un clic de ratón confiable en el centro, con el hit-test fresco delante: la oclusión de la
    descripción dice qué se veía entonces, y esto dice qué hay ahora."""
    prep = await _eval1(cdp, frame_id, PREP, {"i": i, "clear": False})
    if prep.get("gone") or prep.get("detached"):
        return prep
    if not prep.get("hit"):
        return {"err": f"algo tapa el elemento ({prep.get('covered') or 'está fuera del viewport'})"}
    x, y, w, h = prep["rect"]
    where = {"x": x + w / 2, "y": y + h / 2, "button": "left", "clickCount": 1}
    await cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", **where})
    await cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **where})
    return {"ok": 1}


async def _set_files(cdp, frame_id: str, i: int, paths: list[str]) -> None:
    """Funciona sobre un input `display:none` y dispara un `change` de confianza sin selector de ficheros
    —que en Wayland es un portal al que CDP no llega—. Cada ruta pasa por `safe_file` aquí, el único camino a
    `DOM.setFileInputFiles`; dos ficheros sin `multiple` los rechaza quien llama."""
    paths = [safe_file(p) for p in paths]
    await cdp.send("DOM.enable")
    node = await cdp.send("DOM.describeNode", {"objectId": await _object_id(cdp, frame_id, i)})
    await cdp.send("DOM.setFileInputFiles",
                   {"files": paths, "backendNodeId": node["node"]["backendNodeId"]})


async def _combobox(session, cdp, frame_id: str, i: int, want: str) -> str:
    """Teclear, **esperar un predicado y no un tiempo**, recorrer con flechas hasta que
    `aria-activedescendant` nombre la opción que se quiere, y confirmar con Enter."""
    await cdp.send("Input.insertText", {"text": want})
    deadline = time.monotonic() + 3.0
    state = {}
    while time.monotonic() < deadline:
        state = await _eval1(cdp, frame_id, COMBO, {"i": i})
        if state.get("expanded") == "true" or state.get("opts"):
            break
        await asyncio.sleep(0.05)
    opts = state.get("opts") or []
    if not opts:
        return "el listbox no se abrió"
    target = next((n for n, (_id, text) in enumerate(opts) if text.casefold() == want.casefold()),
                  None)
    if target is None:
        target = next((n for n, (_id, text) in enumerate(opts)
                       if text.casefold().startswith(want.casefold())), 0)
    for _ in range(target + 1):
        await session.page.keyboard.press("ArrowDown")
        state = await _eval1(cdp, frame_id, COMBO, {"i": i})
        if state.get("active") == opts[target][0]:
            break
    await session.page.keyboard.press("Enter")
    return ""


async def _fill_one(session, cdp, frame_id: str, row: dict, i: int, want, expect: dict,
                    sent: str = "") -> dict:
    """Una escalera, su relectura y su veredicto. Tres intentos y después es una parada, no un reintento:
    una máscara que rechaza la entrada programática no se arregla insistiendo.

    `expect` es la identidad que el nodo tiene que seguir teniendo. El índice es solo el camino rápido;
    esto es lo que hace que un id viejo nunca pueda escribir en el campo equivocado, igual que los refs
    de `observe` se revalidan por rol y nombre antes de actuar.
    """
    page, kind = session.page, row["k"]
    if row.get("sec"):
        # Everything written into a tier A/B field is remembered before it is written, so an echo
        # elsewhere gets scrubbed even if the readback never comes back.
        sanitize.remember([str(want)])
    got, verdict, used, attempt = {}, "retry", "", 1
    for attempt in range(1, ATTEMPTS + 1):
        prep = await _eval1(cdp, frame_id, PREP,
                            {"i": i, "clear": kind not in TOGGLE and kind != "file"})
        if prep.get("gone") or prep.get("detached"):
            return {"verdict": "detached", "attempts": attempt}
        if attempt == 1 and any(prep.get(k, "") != v for k, v in expect.items()):
            raise StaleHandle(
                f"el campo {expect} ya no es el que se describió (ahora es "
                f"{ {k: prep.get(k) for k in expect} }): no se escribe nada. Vuelve a llamar a "
                "form_describe.")

        if kind in TOGGLE:
            if attempt == 1:
                current = (await _eval1(cdp, frame_id, READBACK, {"i": i})).get("chk")
                if bool(current) == _truthy(want):
                    return {"verdict": "already", "attempts": 1, "used": "lectura previa",
                            "value": "sí" if _truthy(want) else "no"}
            used = ("foco + Espacio (confiable, sin geometría)", "clic en la etiqueta",
                    "clic de ratón confiable en el centro")[attempt - 1]
            if attempt == 1:
                await page.keyboard.press("Space")
            elif attempt == 2:
                if (await _eval1(cdp, frame_id, CLICK_LABEL, {"i": i})).get("err"):
                    continue
            else:
                clicked = await _click(cdp, frame_id, i)
                if clicked.get("err"):
                    return {"verdict": "failed", "attempts": attempt, "msg": clicked["err"],
                            "used": used}
        elif kind in SELECTISH:
            used = ("selección en el DOM + eventos (lo que hace Playwright)",
                    "teclas de confianza")[min(attempt - 1, 1)]
            if attempt == 1:
                res = await _eval1(cdp, frame_id, SELECT_OPTION, {"i": i, "v": want})
                if res.get("err") == "no_option":
                    return {"verdict": "needs_different_value", "attempts": attempt, "used": used,
                            "msg": f"«{want}» no es una opción de este desplegable",
                            "options": res.get("sample")}
            else:
                for _ in range(max(1, len(str(want)))):
                    await page.keyboard.press("ArrowDown")
        elif kind in DATEISH:
            # No typing ladder here: insertText gives '' and per-key gives garbage, because the digits
            # cascade into the next segment in the BROWSER UI's order, which is not yours.
            used = "valor ISO por el setter nativo + input + change (aquí no se puede teclear)"
            res = await _eval1(cdp, frame_id, SET_NATIVE, {"i": i, "v": str(want)})
            if res.get("err"):
                return {"verdict": "failed", "attempts": attempt, "msg": res["err"], "used": used}
        elif kind == "file":
            used = "DOM.setFileInputFiles sobre el input escondido"
            paths = [want] if isinstance(want, str) else list(want)
            await _set_files(cdp, frame_id, i, paths)
        elif kind == "combobox":
            used = "teclear, esperar el listbox, flechas hasta aria-activedescendant, Enter"
            problem = await _combobox(session, cdp, frame_id, i, str(want))
            if problem:
                used += f" ({problem})"
        else:
            value = str(_wanted(row, want))
            used = ("Input.insertText (confiable)", "tecla a tecla (confiable)",
                    "setter del prototipo nativo + eventos")[attempt - 1]
            if attempt == 1:
                await cdp.send("Input.insertText", {"text": value})
            elif attempt == 2:
                await page.keyboard.type(value, delay=0)
            else:
                res = await _eval1(cdp, frame_id, SET_NATIVE, {"i": i, "v": value})
                if res.get("err"):
                    return {"verdict": "failed", "attempts": attempt, "msg": res["err"], "used": used}
            await _eval1(cdp, frame_id, BLUR, {"i": i})

        await asyncio.sleep(SETTLE)
        got = await _eval1(cdp, frame_id, READBACK, {"i": i})
        verdict = _verdict(row, want, got)
        if verdict != "retry":
            break
        # browser-use's concatenation case: the readback is longer than the intent and contains it, so
        # the field was never cleared. Clearing through the native setter is the fix.
        text = str(got.get("v", ""))
        if text and str(want) in text and len(text) > len(str(want)):
            await _eval1(cdp, frame_id, SET_NATIVE, {"i": i, "v": ""})

    out = {"verdict": verdict, "attempts": attempt, "used": used, **_shown(row, got, sent)}
    if verdict == "retry":
        short = len(str(got.get("v", ""))) < len(str(_wanted(row, want)))
        if kind in TOGGLE:
            out["verdict"] = "unverifiable"
            out["msg"] = ("el estado de este control no cambió con ninguna de las tres técnicas, así "
                          "que puede vivir en otro sitio (un input hermano oculto, o solo el estado del "
                          "framework). Mira la captura; no afirmes que está hecho.")
        else:
            out["verdict"] = "failed"
            out["msg"] = ("la máscara del campo está rechazando la entrada programática: tras los tres "
                          "intentos la relectura sigue saliendo corta. Es una parada, no un reintento: "
                          "pídeselo al usuario." if short else
                          "tres técnicas y la relectura sigue sin cuadrar. Mira la captura: puede que el "
                          "foco acabe en otro campo o que el widget guarde su estado en otro sitio.")
    return out


def _diff(before: list[tuple], after: list[tuple]) -> dict:
    """El diff de campos entre tandas: lo que apareció es la señal de progreso de un asistente."""
    def live(frames):
        return {f"{r.get('label', '')}|{r.get('nm', '')}|{r['k']}"
                for _f, _h, d in frames for r in d["fields"] if not r.get("hidden")}
    old, new = live(before), live(after)
    return {"appeared": sorted(new - old), "vanished": sorted(old - new)}


async def fill(session, fields: list[dict], autofill_profile: bool = False, dry_run: bool = False,
               filename: str = "") -> dict:
    """El bucle de rellenar y verificar, entero en el servidor. **Nunca envía.**

    Devuelve la relectura, la captura, `form_valid`, `blocking`, `changed_fields` y `submit_tokens`:
    uno por cada botón de envío visible, cada uno con su clasificación, que son el único camino a un
    clic de envío.
    """
    session.require_no_dialog()
    if not _fields:
        raise StaleHandle("no hay ninguna descripción de formulario viva. Llama antes a form_describe.")
    if session.page.url != _url:
        raise StaleHandle(f"la página cambió desde la descripción ({_url} -> {session.page.url}), así que "
                        "todos los ids murieron. Vuelve a llamar a form_describe.")
    t0 = time.perf_counter()

    plan, needs, human = [], [], []
    for item in fields:
        field = _get(item.get("id", item.get("i")))
        row = field.row
        raw = item.get("value", item.get("option", item.get("checked", item.get("file"))))
        if row.get("pay"):
            human.append({"i": field.fid, "label": row.get("label", ""), "reason": "payment",
                          "next": "es un campo de pago en un iframe de pasarela y no se rellena. Deja "
                                  "el resto listo y pásaselo al usuario."})
            continue
        if row.get("sec") == "A":
            human.append(_handoff(field))
            continue
        value = resolve(raw)             # y `resolve` memoriza lo que sustituye, antes de comprobarlo:
                                         # un `check` que falla CITA el valor (««ES91…» no cumple
                                         # pattern=…») y el `scrub` del final tiene que poder tacharlo
        if row.get("sec"):
            # Lo que el MODELO teclea en un campo marcado, que `resolve` no ha visto pasar.
            sanitize.remember([str(value)])
        problem = _format_checks(row, value)
        entry = {"i": field.fid, "label": row.get("label", ""), "k": row["k"],
                 "check": problem or "ok"}
        field.sent = raw if isinstance(raw, str) and "{{" in raw else ""
        if field.sent:
            entry["sent"] = field.sent   # la clave, nunca el valor resuelto
        if not problem:
            entry["field"], entry["value"] = field, value
        plan.append(entry)

    asked = {p["i"] for p in plan} | {h["i"] for h in human}
    for fid, field in _fields.items():
        row = field.row
        if fid in asked or row.get("hidden") or row.get("dis") or row.get("ro"):
            continue
        # Un campo de nivel A **vacío** es un traspaso aunque no sea obligatorio: el formulario no se
        # puede acabar sin una persona y decirlo ahora es más barato que descubrirlo al enviar. Si ya
        # trae algo, lo puso una persona y no hay nada que pedir.
        if row.get("sec") == "A":
            if not row.get("vl") and not row.get("v"):
                human.append(_handoff(field))
            continue
        if row.get("valid") != 0 or not row.get("req"):
            continue
        needs.append({"i": fid, "label": row.get("label", ""), "k": row["k"],
                      **{k: row[k] for k in ("pattern", "min", "max", "maxlength", "minlength",
                                             "section", "why") if row.get(k)},
                      **({"format": ISO_FORMAT[row["k"]]} if row["k"] in ISO_FORMAT else {}),
                      **({"options": [{"v": v, "t": t} for v, t in row["opts"][:12]]}
                         if row.get("opts") else {}),
                      "next": "es obligatorio y el plan no lo trae. No inventes un valor: pregúntaselo "
                              "al usuario."})

    if dry_run:
        return sanitize.scrub({
            "dry_run": True,
            "plan": [{k: v for k, v in p.items() if k not in ("field", "value")} for p in plan],
            "needs_value": needs, "human_required": human,
            "blocked": [[p["i"], p["check"]] for p in plan if p["check"] != "ok"],
            "next": "no se ha escrito nada en la página. Arregla los `check` que no digan ok y vuelve a "
                    "llamar sin dry_run."})

    report_autofill = None
    async with Frames(session) as frames:
        before = await _harvest(frames, viewport_only=False)
        if autofill_profile:
            report_autofill = await _autofill(session, frames, before)
            before = await _harvest(frames, viewport_only=False)

        # DOM order, except that a select which aria-controls another field goes first: province before
        # city, country before postcode. Knowable server-side, so the model never spends a turn on it.
        ordered = sorted((p for p in plan if "field" in p),
                         key=lambda p: (0 if p["field"].row.get("controls") else 1, p["i"]))
        readback, per_field = [], []
        for item in ordered:
            check(session)  # Meta+Shift+B (o el freno) a media tanda: no se escribe ni un campo más
            field, row, want = item["field"], item["field"].row, item["value"]
            cdp = frames.of(field.frame_id)
            index, extra = field.i, {}
            if row.get("group"):
                # One field, N nodes: filling the group means focusing the member that holds the value.
                where = next((n for n, (v, t) in enumerate(row["opts"])
                              if v == str(want) or t.strip().casefold() == str(want).strip().casefold()),
                             None)
                if where is None:
                    readback.append({"i": field.fid, "label": row.get("label", ""), "k": row["k"],
                                     "verdict": "needs_different_value",
                                     "msg": f"«{want}» no es una opción del grupo",
                                     "options": [{"v": v, "t": t} for v, t in row["opts"]]})
                    continue
                index, want, extra = row["ix"][where], True, {"value": row["opts"][where][1]}
            expect = ({"nm": row.get("nm", "")} if row.get("group")
                      else {"nm": row.get("nm", ""), "eid": row.get("id", "")})
            t1 = time.perf_counter()
            try:
                got = await _fill_one(session, cdp, field.frame_id, row, index, want, expect,
                                      field.sent)
            except (UserDriving, Cancelled):
                raise
            except Exception as exc:        # a field that blows up is one row, not the whole batch
                got = {"verdict": "failed", "attempts": 1, "msg": str(exc)[:200]}
            ms = (time.perf_counter() - t1) * 1000
            per_field.append(ms)
            readback.append({"i": field.fid, "label": row.get("label", ""),
                             "section": row.get("section", ""), "k": row["k"],
                             "ms": round(ms, 1), **got, **extra})

        after = await _harvest(frames, viewport_only=False)
        filled = {r["i"] for r in readback}
        by_node = {(f.frame_id, f.i): f for f in _fields.values()}
        for frame_id, _host, data in after:
            for row in data["fields"]:
                field = by_node.get((frame_id, row["i"]))
                if field is None:
                    continue
                row["fid"] = field.fid
                field.row = row            # keep every id pointing at the live node and its live state
                if field.fid in filled or row.get("dis"):
                    continue
                readback.append({"i": field.fid, "label": row.get("label", ""),
                                 "section": row.get("section", ""), "k": row["k"],
                                 "verdict": "not_filled",
                                 # Un campo que el agente NO rellenó no lo sabe el servidor, así que
                                 # aquí no aplica la excepción de la relectura: vale la regla de
                                 # siempre, la longitud de un texto libre y el valor solo de lo
                                 # estructural. Es la misma de `_public`, que es la de `observe`.
                                 # Salvo que lo rellenara el servidor desde el perfil en una tanda
                                 # anterior: entonces el valor sigue siendo un dato que el modelo no
                                 # tiene, y una fecha del perfil es justo lo «estructural» que se
                                 # emitiría en claro por esta rama.
                                 **({"len": len(str(_render(row) or "")) or row.get("vl", 0),
                                     "sent": field.sent} if field.sent
                                    else {"len": row.get("vl", 0), "sec": row["sec"]} if row.get("sec")
                                    else {"len": row["vl"]} if "vl" in row
                                    else {"value": _render(row)}),
                                 **{k: row[k] for k in ("valid", "why", "msg")
                                    if row.get(k) is not None}})
        gate = await _gate(frames)
        # UN token por botón de envío visible, cada uno con su botón, su clasificación y su huella.
        # Antes se acuñaba solo para el primero en orden del DOM —el botón por defecto del formulario,
        # el que pulsaría un Enter— y entonces, en el formulario de dos botones más común de internet
        # («Guardar borrador» y «Enviar»), el de verdad no se podía pulsar por esta tool. Elegir por el
        # servidor «el irreversible» sería peor: en un asistente se saltaría el `Siguiente`. Decide el
        # usuario, y la clasificación viaja en cada token para que `form_submit` la exija.
        _tokens.clear()        # los de la tanda anterior ya no valen: se reacuña el juego entero
        tokens = [_mint(btn, await _fingerprint(frames, btn)) for btn in gate["buttons"]]

    shot = await observe.screenshot(session, filename) if filename else None
    return sanitize.scrub({
        "readback": sorted(readback, key=lambda r: r["i"]),
        "screenshot": shot,
        "form_valid": gate["valid"],
        "blocking": gate["bad"],
        "changed_fields": _diff(before, after),
        "needs_value": needs,
        "human_required": human,
        "blocked": [[p["i"], p["check"]] for p in plan if p["check"] != "ok"],
        "autofill": report_autofill,
        "submit_tokens": tokens,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
        "ms_per_field": round(sum(per_field) / len(per_field), 1) if per_field else 0.0,
        "next": ("form_fill no envía nunca. Repasa la relectura con el usuario y, si da el visto bueno, "
                 "llama a form_submit con el token del botón que quiera pulsar: hay uno por cada botón "
                 "de envío visible, con su clasificación.") if tokens else
                ("form_fill no envía nunca, y aquí no hay botón de envío de formulario (el botón que "
                 "manda es un `button` suelto, sin <form>), así que form_submit no aplica: con el "
                 "visto bueno del usuario a ese envío —pedirlo en la tarea ya lo es—, púlsalo con "
                 "browser_act (`click`) y comprueba el resultado con browser_observe."),
    })


def _handoff(field: Field) -> dict:
    row = field.row
    return {"i": field.fid, "label": row.get("label", ""), "len": row.get("vl", 0),
            "reason": "password" if row["k"] == "password" else "card_or_otp",
            "next": "las contraseñas, los números de tarjeta, los CVC y los códigos de un solo uso no "
                    "los teclea el agente nunca, y el servidor no los guarda. Rellena todo lo de "
                    "alrededor, para, y pásale la ventana al usuario con browser_handoff(reason) "
                    "para que lo escriba él: una compra o una transferencia es siempre una operación a dos."}


async def _gate(frames: Frames) -> dict:
    """La puerta previa al envío, que es gratis y es obligatoria."""
    buttons = [b for b in _buttons if b["submit"] and not b["hidden"]]
    valid, bad = True, []
    anchors = buttons or [{"frame": f.frame_id, "i": f.i} for f in list(_fields.values())[:1]]
    for btn in anchors:
        got = await _eval1(frames.of(btn["frame"]), btn["frame"], GATE, {"i": btn["i"]})
        if got.get("gone") or got.get("detached"):
            continue
        btn["action"], btn["method"] = got["action"], got["method"]
        valid = valid and bool(got["valid"])
        btn.setdefault("t", got["t"])
        for row in got["bad"]:
            if row not in bad:
                bad.append(row)
    return {"valid": valid, "bad": bad, "buttons": buttons}


async def _fingerprint(frames: Frames, btn: dict) -> str:
    got = await _eval1(frames.of(btn["frame"]), btn["frame"], FINGERPRINT, {"i": btn["i"]})
    return hashlib.sha256(str(got.get("fp", "")).encode()).hexdigest()[:16]


def _mint(btn: dict, fingerprint: str) -> dict:
    """Un nonce atado a (url, botón, huella de todo lo relleno). De un solo uso, caduca a los 10 minutos,
    y antes de pulsar se recalcula la huella: así lo que se envía es demostrablemente lo que se mostró."""
    token = _nonce.token_urlsafe(16)
    _tokens[token] = {"url": _url, "btn": btn, "fp": fingerprint, "born": time.monotonic()}
    return {"token": token, "button": btn["t"], "classification": btn["cls"],
            "expires_in_s": int(TOKEN_TTL),
            "next": ("un botón irreversible necesita el visto bueno explícito del usuario antes de "
                     "llamar a form_submit, citando el nombre del botón y lo que se va a mandar. El "
                     "usuario también puede pulsarlo él: pásale la ventana con browser_handoff(reason)."
                     if btn["cls"] == "irreversible" else
                     "llama a form_submit(token) cuando el usuario lo apruebe")}


# --- Autofill ----------------------------------------------------------------------------------------

async def _autofill(session, frames: Frames, harvested) -> dict:
    """Peldaño uno: UNA llamada CDP rellena el bloque de identidad y dirección con el clasificador propio
    de Chrome, con eventos de confianza, sobre etiquetas en español sin `autocomplete`.

    Es destructivo —sobrescribe sin avisar lo que ya hubiera en un campo que clasifique— así que se saca
    una foto antes y se devuelve el diff. Y `trigger` reporta `autofillType` de TODO el formulario, no
    solo de lo que rellenó, así que la misma llamada es el clasificador gratis para el bucle.
    """
    address = autofill_address()
    if not address:
        return {"ok": False, "next": f"el perfil está vacío: escribe {PROFILE_PATH} a partir de "
                                     "profile.example.toml."}
    anchor = next(((fid, row) for fid, _h, data in harvested for row in data["fields"]
                   if row["k"] in ("text", "email", "tel") and not row.get("hidden")
                   and not row.get("sec")), None)
    if anchor is None:
        return {"ok": False, "next": "no hay ningún campo de texto visible al que anclar el Autofill."}
    frame_id, row = anchor
    cdp = frames.of(frame_id)
    async def snap() -> dict:
        out = {}
        for fid, _host in frames.order:
            out |= await _eval1(frames.cdp[fid], fid, VALUES) or {}
        return out

    before = await snap()

    events: list[dict] = []
    # Una función de Python, no `events.append`: Playwright le cuelga un atributo al manejador y un
    # método builtin no tiene dónde ponerlo.
    def collect(event):
        events.append(event)

    cdp.on("Autofill.addressFormFilled", collect)
    try:
        await cdp.send("DOM.enable")
        await cdp.send("Autofill.enable")
        node = await cdp.send("DOM.describeNode",
                              {"objectId": await _object_id(cdp, frame_id, row["i"])})
        try:
            # The wait_for is not a nicety: a name that kills the browser NEVER returns through
            # Playwright — the await simply hangs (measured at 8 minutes) with the session lock held.
            await asyncio.wait_for(cdp.send("Autofill.trigger", {
                "fieldId": node["node"]["backendNodeId"], "frameId": frame_id,
                "address": {"fields": address}}), AUTOFILL_TIMEOUT)
        except asyncio.TimeoutError:
            await after_crash(session)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200],
                    "next": ("Chrome no ha parseado ese formulario: `Autofill.trigger` contesta «Field "
                             "not found» cuando el campo no pertenece a ningún formulario que su "
                             "parseador reconozca —medido con un input suelto sin `<form>` "
                             "(research/p2/E-autofill.md §10)—. "
                             if "Field not found" in str(exc) else "")
                            + "Rellena sin autofill_profile: el bucle determinista cubre el "
                              "formulario entero."}
        await asyncio.sleep(0.15)
    finally:
        try:
            cdp.remove_listener("Autofill.addressFormFilled", collect)
        except Exception:
            pass

    after = await _harvest(frames, viewport_only=False)
    now = await snap()
    # Only `filledFields`, which is about the page. `addressUi` echoes the whole address back, including
    # what matched nothing: it is a mirror of the profile, not a report of the page.
    classified = [(f.get("name") or f.get("id"), f.get("autofillType", ""), f.get("fillingStrategy"))
                  for e in events for f in e.get("filledFields", ())]
    return {"ok": True,
            "names_sent": [f["name"] for f in address],
            "classified": [[n, t, s] for n, t, s in classified if t],
            "overwrote": [n for n, v in before.items() if v and now.get(n) != v],
            "note": "el Autofill sobrescribe sin avisar lo que clasifica y puede normalizar los valores, "
                    "así que la relectura compara normalizado y este diff dice qué pisó."}


def crashed(session) -> bool:
    """Si el proceso del navegador se murió. Las tres señales que se midieron juntas tras la caída: el
    cgroup vacío, Playwright desconectado y ninguna página."""
    try:
        browser = session.context.browser
        if browser is not None and not browser.is_connected():
            return True
        return not session.context.pages
    except Exception:
        return True


async def after_crash(session) -> None:
    """El `Autofill.trigger` que no vuelve. Mató el proceso del **navegador**, no el renderer, y por eso
    `session.recover()` no se llama aquí: está escrita para una caída del renderer —la conexión del
    navegador sobrevive y se crea otro target por ahí— y con el navegador muerto falla en su primera
    línea, porque no hay página que devolver. Medido: 11 procesos del cgroup a 0, el cgroup borrado,
    KWin sin la ventana y Playwright desconectado (research/p2/E-autofill.md §6). Lo único que funciona
    es `close()` y una sesión nueva, y eso lo decide el usuario, no el servidor: aquí se dice.

    Por eso la whitelist de 54 nombres es una frontera de seguridad y no una comodidad: de esta caída no
    hay vuelta barata, así que no puede ocurrir.
    """
    reset()
    observe.reset()
    raise BrowserCrashed(
        "Autofill.trigger no volvió en "
        f"{AUTOFILL_TIMEOUT:.0f} s y el navegador está "
        + ("muerto" if crashed(session) else "en un estado desconocido") +
        ": el estado de la página se ha perdido (las cookies del perfil siguen ahí; un formulario a "
        "medio rellenar no). No reintentes y no esperes: llama a browser_close, luego a browser_open, y "
        "rellena sin autofill_profile, que el bucle determinista cubre el formulario entero.")


# --- enviar ------------------------------------------------------------------------------------------

async def submit(session, token: str, expect: str = "any", timeout: float = 20.0,
                 approved: bool = False) -> dict:
    """El único camino a un clic de envío. Exige el token, pasa la puerta y espera el conjunto de
    evidencias. Si ninguna evidencia salta, el resultado es `unknown` y nunca `success`: un «enviado»
    falso y seguro es el fallo más caro de todo el sistema.

    **El token lleva la clasificación y aquí se actúa sobre ella.** Si estuviera solo en el payload de
    `form_describe` habría un agujero: un modelo que vaya directo a `form_fill` con ids de un turno
    anterior consigue un token sin haber visto nunca la palabra «irreversible». Así que un token
    irreversible se niega la primera vez, nombrando el botón y lo que se va a mandar, y **no se gasta**:
    el modelo le pide el visto bueno al usuario y vuelve con `approved=true`.
    """
    session.require_no_dialog()
    record = _tokens.get(token)
    if record is None:
        raise FormError("ese submit_token no existe, ya se usó, o se acuñó otro después. Es de un solo "
                        "uso: vuelve a llamar a form_fill y usa el que devuelva.")
    if time.monotonic() - record["born"] > TOKEN_TTL:
        _tokens.pop(token, None)
        raise FormError("el submit_token ha caducado (10 minutos). Vuelve a llamar a form_fill, repasa "
                        "la relectura y confírmala con el usuario.")
    btn = record["btn"]
    if btn["cls"] == "irreversible" and not approved:
        return {"status": "needs_approval", "url": sanitize.clean_url(record["url"]),
                "http_status": None, "confirmation_id": "", "message": "",
                "errors": [], "artifacts": {"button": btn["t"], "classification": btn["cls"],
                                            "action": btn.get("action", ""),
                                            "method": btn.get("method", "")},
                "next": f"«{btn['t']}» está clasificado irreversible y no se ha pulsado nada. El token "
                        "sigue vivo. Pídele al usuario el visto bueno explícito citando el nombre del "
                        "botón y lo que se va a mandar (la relectura de form_fill), y vuelve con "
                        "form_submit(token, approved=true). El usuario también puede pulsarlo él: pásale "
                        "la ventana con browser_handoff(reason), que es la salida buena si hay dudas."}
    _tokens.pop(token, None)                    # de un solo uso, y se gasta al pulsar de verdad
    if session.page.url != record["url"]:
        raise FormError(f"TokenStale: la página era {record['url']} y ahora es {session.page.url}, así "
                        "que no se pulsa nada. Vuelve a describir y a rellenar.")
    page = session.page

    async with Frames(session) as frames:
        cdp = frames.of(btn["frame"])
        gate = await _eval1(cdp, btn["frame"], GATE, {"i": btn["i"]})
        if gate.get("gone") or gate.get("detached"):
            raise FormError("TokenStale: el botón de envío ya no está en la página. Vuelve a describir.")
        if gate["t"] != btn["t"]:
            raise FormError(f"TokenStale: el botón era «{btn['t']}» y ahora dice «{gate['t']}». No se "
                            "pulsa nada. Vuelve a describir y a rellenar.")
        if not gate["valid"]:
            return sanitize.scrub({
                "status": "refused", "url": sanitize.clean_url(page.url), "http_status": None,
                "confirmation_id": "", "message": "", "errors": gate["bad"], "artifacts": {},
                "next": "el formulario no es válido y no se ha pulsado nada. Los campos que lo bloquean "
                        "están en `errors` como [campo, por qué, mensaje] — ojo a los que estén en un "
                        "paso oculto. Arréglalos con form_fill y vuelve."})
        if await _fingerprint(frames, btn) != record["fp"]:
            raise FormError("TokenStale: algo cambió en el formulario entre la relectura y el envío, así "
                            "que lo que se enviaría no es lo que se mostró. Vuelve a llamar a form_fill.")
        action = urljoin(page.url, gate["action"] or "")
        key = (action, record["fp"])
        if time.monotonic() - _submitted.get(key, 0.0) < DOUBLE_SUBMIT:
            raise FormError("este mismo formulario, con estos mismos valores, se envió hace menos de "
                            "tres minutos. No se envía dos veces: comprueba el resultado del primero.")

        responses: list[list] = []
        navigations: list[str] = []
        on_response = lambda r: responses.append([r.request.method, r.url, r.status])
        on_nav = lambda f: navigations.append(f.url) if f is page.main_frame else None
        page.on("response", on_response)
        page.on("framenavigated", on_nav)
        hit, gone, extra = None, False, {}
        try:
            clicked = await _click(cdp, btn["frame"], btn["i"])
            if clicked.get("err"):
                raise FormError(f"no se pudo pulsar «{btn['t']}»: {clicked['err']}. No se ha enviado "
                                "nada.")
            _submitted[key] = time.monotonic()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                if session.dialog is not None:
                    return sanitize.scrub({
                        "status": "dialog", "url": sanitize.clean_url(page.url), "http_status": None,
                        "confirmation_id": "", "message": session.dialog.message, "errors": [],
                        "artifacts": {"requests": responses[-10:]},
                        "next": "el envío abrió un diálogo nativo y la página está bloqueada en él, que "
                                "es además una señal de irreversibilidad. Pregúntale al usuario y "
                                "respóndelo con browser_dialog(accept=true|false)."})
                hit = next((r for r in responses if r[1].split("#")[0] == action.split("#")[0]), None)
                gone = bool((await _eval1(cdp, btn["frame"], NODE_GONE, {"i": btn["i"]})).get("gone"))
                try:
                    extra = await _eval1(cdp, btn["frame"], CONFIRM_JS) or {}
                except Exception:
                    extra = {}             # a navigation mid-flight kills the world; that is evidence
                if expect == "navigation" and not navigations:
                    continue
                if expect == "message" and not extra.get("live"):
                    continue
                if hit or navigations or gone or extra.get("live") or extra.get("confirmation"):
                    break
        finally:
            page.remove_listener("response", on_response)
            page.remove_listener("framenavigated", on_nav)

    # El bucle rompe con la PRIMERA evidencia, y la primera suele ser el 303 o la navegación: en ese
    # instante la página de aterrizaje todavía no existe, así que el identificador de confirmación se
    # lee otra vez cuando ya ha cargado. Es el dato que el usuario necesita después, y leerlo pronto es
    # leerlo vacío. Sesión de frames nueva a propósito: la navegación se llevó por delante la anterior.
    if hit or navigations:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            async with Frames(session) as landed:
                frame_id = landed.order[0][0]
                extra = await _eval1(landed.of(frame_id), frame_id, CONFIRM_JS) or extra
        except Exception:
            pass                  # sin confirmación legible el estado sigue siendo el de la evidencia

    http = hit[2] if hit else None
    if http is not None and http >= 400:
        status = "failed"
    elif (http is not None) or navigations or extra.get("confirmation") or gone or extra.get("live"):
        status = "success"
    else:
        status = "unknown"
    reset()                 # after a submit every field id is a guess: the page is not what it was
    return sanitize.scrub({
        "status": status,
        "url": sanitize.clean_url(page.url),
        "http_status": http,
        "confirmation_id": extra.get("confirmation", ""),
        "message": "; ".join(extra.get("live", ())),
        "errors": [],
        "artifacts": {"requests": [[m, sanitize.clean_url(u), s] for m, u, s in responses[-10:]],
                      "navigations": [sanitize.clean_url(u) for u in navigations]},
        "next": ("ninguna evidencia saltó en el plazo: ni estado HTTP de la acción, ni navegación, ni "
                 "formulario desaparecido, ni mensaje nuevo. NO lo des por enviado: observa la página y "
                 "dile al usuario qué ves." if status == "unknown" else
                 "guarda el confirmation_id: es lo que el usuario necesita después."),
    })
