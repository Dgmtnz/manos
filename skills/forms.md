---
name: forms
description: Rellenar un formulario web con form_describe, form_fill y form_submit — la escalera por tipo de campo, las tres cosas que corrompen datos en silencio, qué valor se puede mostrar y cuál no, y cuándo parar y pedírselo al usuario.
---

Todo lo que hay aquí está medido sobre esta máquina. Donde se da un número o una salida, es la real.

Esta skill va **dentro** del método general: la tarea empieza con `task_checkpoint(accept=…)` y acaba
con un `submit_task_complete` aceptado antes de responder, también cuando la orden es «no lo envíes».
`submit_task_complete` no envía el formulario: certifica el trabajo (`form_submit` es lo que envía).

## La escalera. Párate en el primer peldaño que sirva

**Antes del peldaño 0:** si `task_checkpoint` (`replays`) o `browser_open` (`next`) dicen que este formulario
ya se rellenó en una tarea certificada, `task_replay(name)` lo rellena sin pensarlo, campo por campo por su
etiqueta, y para en la primera diferencia o antes de un envío irreversible. Luego `form_describe` para ver
y contar qué quedó, y sigue a mano desde `stopped_at` si paró.

**0. `form_describe` primero, siempre.** Es la única tool que acuña los ids de campo, cuesta ~4,6 KB
para un formulario de 24 campos y trae lo que el árbol de accesibilidad demostrablemente no tiene:
`type`, `pattern`, `maxlength`, `min`, `max`, `step`, `inputmode`, el `autocomplete` de HTML, los
*valores* de las `<option>` y la validez real. Llámala sin miedo: es barata y sin ella estás adivinando
el formato.

**1. El Autofill de Chrome para identidad y dirección.** Una sola llamada rellena el bloque entero con
eventos de confianza, usando el clasificador propio del navegador. Funciona con etiquetas en español y
sin ningún atributo `autocomplete`, sobre `<select>` y sobre inputs controlados de React. Lo pide
`form_fill(autofill_profile=true)`. Dos cosas que hay que saber antes de usarlo:

- **Su alcance es dirección e identidad y nada más.** Nombre, apellidos (tiene modelo de dos
  apellidos), email, teléfono, vía, número, piso, municipio, provincia, código postal y país. Deja
  intactos el DNI, la fecha de nacimiento, el IBAN, los comentarios, las casillas de consentimiento y
  los ficheros: sobre un formulario de 9 campos rellenó 2. El resto lo hace el peldaño 2.
- **Es destructivo.** Sobrescribe sin avisar lo que ya hubiera en un campo que clasifique
  (`"NO TOCAR"` → `"Diego"`), y por eso `form_fill` te devuelve el diff de lo que pisó. No lo lances
  sobre un formulario que el usuario ya empezó a rellenar sin mirar ese diff. Lo que **no** hace es
  normalizar: se midió que cambiaba `600112233` → `+34600112233` y `ES` → `"Spain"`, y sobre Chrome 153
  **no se reproduce** —los valores vuelven exactamente como se mandaron—. La relectura compara
  normalizado igual, porque las máscaras de la propia página sí reformatean.

**2. Las técnicas por widget**, que corren dentro de `form_fill` con hasta tres intentos por campo.
Tú no las ejecutas: las conoces para saber qué significa un fallo y qué pedir. Están abajo.

**3. El traspaso.** Captcha, contraseña, tarjeta, OTP: `browser_handoff(reason)` y esperar a que Diego
diga que ha terminado (`browser_resume`). Un valor que es una decisión: pararse y pedirlo. Es un resultado
válido y es más barato que insistir.

## Nunca asignes `.value` ni `.checked`

Medido sobre React 18: `el.value='Diego'` más un `new Event('input')` deja el estado del componente
**vacío**, y la divergencia hace que **el siguiente clic real calcule el valor equivocado**. Con
`el.checked = true` pasa lo mismo: el estado se queda en `false` y el clic siguiente lo pone al revés.
No es que no funcione, es que **envenena la acción correcta posterior**: corrupción de datos en
silencio. Por eso `form_fill` teclea con eventos de confianza y, cuando tiene que asignar, lo hace por el
setter del prototipo nativo.

## En seis tipos de campo no se puede teclear

`date`, `time`, `month`, `week`, `datetime-local`, `color` y `range` no son campos de texto: son
compuestos de shadow DOM con segmentos. Medido sobre un `input type=date` en una página en español:

```
Input.insertText('25/12/1990')  -> value ''                 sin ningún evento
tecla a tecla '25121990'        -> value '121990-02-05'      basura
tecla a tecla '12251990'        -> value ''
valor ISO + input + change      -> value '1990-12-25'        FUNCIONA
```

El `121990` es el año: los dígitos sobrantes cascadean al segmento siguiente en el orden de la **UI del
navegador**, que no es el tuyo. Así que:

- **Da siempre el valor en ISO**: `date` → `AAAA-MM-DD`, `time` → `HH:MM`, `month` → `AAAA-MM`,
  `week` → `AAAA-W##`, `datetime-local` → `AAAA-MM-DDTHH:MM`.
- **El locale es `es_ES` y las fechas se escriben día/mes/año, así que la conversión es tu trabajo**:
  «3 de febrero de 1991» y «3/2/1991» son `1991-02-03`. Nunca produzcas la cadena localizada; el
  servidor te la devuelve ya renderizada como `03/02/1991` en la relectura, para que el usuario la lea.
- **Nunca pulses el calendario.** El selector nativo es UI del navegador y no se puede clicar (misma
  clase que el desplegable de abajo); uno pintado en JS sí, pero cuesta N clics de navegación de mes y
  es donde los agentes pierden más pasos. Teclear el valor es una acción con verificación determinista.
- **El otro animal, muy común en las administraciones españolas**: un `type=text` que **espera**
  `dd/mm/aaaa`. Se reconoce por `pattern` (`\d{2}/\d{2}/\d{4}`), por el `placeholder` (`dd/mm/aaaa`),
  por `maxlength=10` o por la etiqueta. Ahí sí va el formato local. Si el `form_describe` no te deja
  decidirlo, manda ISO, mira la relectura, y si aparece error reintenta con `dd/mm/aaaa`.

## El desplegable de un `<select>` nativo es UI del navegador

Los rects de sus `<option>` son **0x0** y ningún evento de ratón los alcanza. No planifiques nunca un
clic sobre una opción de un desplegable nativo. `form_fill` lo conduce por selección en el DOM más
eventos (que es lo que hace Playwright, y el `onChange` de React sí salta ahí) o por teclas de
confianza. Si en una captura ves un desplegable nativo abierto, el agente está atascado: `Escape`.

## La validez la dice el navegador, y la propiedad del AX está mal

La API de validación de restricciones (`validity`, `checkValidity()`, `validationMessage`) es la verdad
sobre el terreno: es el navegador diciéndote por qué un campo está mal, con sus propias palabras.
**La propiedad `invalid` del árbol de accesibilidad está mal para los inputs de texto** — dijo `false`
sobre un campo que era `:invalid`. Nunca decidas con ella.

El `why` que te llega es uno de `valueMissing`, `typeMismatch`, `patternMismatch`, `tooLong`,
`tooShort`, `rangeUnderflow`, `rangeOverflow`, `stepMismatch`, `badInput`, `customError`. Lee la tabla
así:

| lo que dice la relectura | qué significa | qué haces |
|---|---|---|
| el valor cuadra y `valid` | hecho | siguiente |
| el valor cuadra, `valid:false`, `why` ∈ `patternMismatch`/`typeMismatch`/`range*`/`stepMismatch`/`tooLong` | **el valor está mal, no el tecleo** | el servidor ya no reintenta: te toca a ti cambiar el valor, con el `pattern`, el `min/max` y el mensaje que te da |
| el valor difiere pero normaliza igual | una máscara lo reformateó | **éxito**, no es un fallo |
| `valid:true` pero `showing_error` | el HTML dice que vale y el JS del sitio dice que no | créete al sitio: su mensaje visible es el que hay que leer y el que hay que citar al usuario |
| la relectura no se pudo leer | el elemento se desprendió porque un `change` repintó el formulario | **no es un fallo**: se vuelve a describir y, si el campo ya está bien o ya no está, está hecho |

`validationMessage` llega en **inglés** y no hay forma de cambiarlo desde CDP (comprobado de tres
maneras). Tú lo lees igual; lo que le cites al usuario debe ser el texto visible del propio sitio
(`error` en el `form_describe`), no la frase inglesa del navegador.

Antes de enviar, la puerta es gratis: `form_fill` te devuelve `form_valid` y `blocking` con la lista de
`[campo, por qué, mensaje]`. Pilló un campo obligatorio dentro de un paso **oculto** de un asistente.

## Máscaras: la relectura nunca es igual a lo que escribiste, y eso está bien

```
teléfono con máscara:  insertText('600112233')  -> '600 11 22 33'
importe en es-ES:      insertText('123456')     -> '1234,56 €'
```

Las máscaras no rompen el tecleo, rompen la **verificación**. La comparación es normalizada: teléfono
sin espacios y sin `+34`, importe solo dígitos (en es-ES la coma es el decimal), documentos (DNI, NIE,
IBAN) en mayúsculas y sin espacios ni guiones. Si tras los tres intentos la relectura sigue saliendo
**corta**, la máscara está rechazando la entrada programática: es una parada, no un reintento.

## Casillas, radios y asistentes

- **Lee antes de escribir.** Si la casilla ya está como la quieres, no la toques: el bug más común de un
  agente en un formulario es volver a pulsar una casilla que ya estaba bien. `form_fill` lo informa como
  `already`.
- Un grupo de radios es **un** campo con opciones, no N campos booleanos. No pidas dos a `true`.
- Un `div[role=checkbox]` puede no tener teclado: se verifica por `aria-checked`, y si no cambia, el
  estado real puede vivir en otro sitio. Entonces te llega `unverifiable` y lo que hay que hacer es
  mirar la captura, no afirmar que está hecho.
- **Asistentes y campos que aparecen después.** Un campo que solo se revela cuando el anterior valida
  sale en el `form_describe` marcado `hidden` **antes** de existir en pantalla. Es información, no ruido:
  te dice que el formulario tiene más. Después de cada tanda de `form_fill`, `changed_fields` te dice
  qué apareció y qué desapareció. Los indicadores de progreso se leen pero no se creen: lo que marca
  que un paso está completo es `form_valid` y la aparición de un botón nuevo.
- Si el asistente **navega** entre pasos, todos los ids mueren. Un id viejo devuelve `StaleHandle`,
  nunca un relleno equivocado: vuelve a llamar a `form_describe`.

## Un combobox ARIA no es un `<select>`

Teclea la consulta, **espera un predicado y no un tiempo** (`aria-expanded` a `true`, o que aparezcan
opciones en el elemento que dice `aria-controls`, o que acabe la petición de sugerencias), recorre con
flechas hasta que `aria-activedescendant` nombre la opción que quieres, y confirma con `Enter`.
**Verifica por el valor del campo, no porque la lista se haya cerrado**: una lista cerrada con el campo
sin cambiar es un fallo silencioso.

## Ficheros: dos fallos silenciosos

```
ruta inexistente                     -> NO da error; el FileList recibe el nombre con size 0
dos ficheros en un input sin multiple -> se queda con el primero, sin avisar
```

Da rutas **absolutas y reales**, mira el `accept` y el `multiple` del `form_describe` antes de mandar, y
comprueba el recuento de `files` en la relectura. El input suele estar oculto detrás de un botón
bonito: eso no es problema, el campo oculto es el que se rellena, y el `form_describe` te dice qué botón
lo abre (`opened_by`).

## El contrato de confirmación

`form_fill` **nunca envía**. Devuelve la relectura, la captura, `form_valid`, `blocking` y
`submit_tokens`: **uno por cada botón de envío visible**, cada uno con el nombre del botón y su
clasificación, porque el formulario de dos botones más común que hay —«Guardar borrador» y
«Enviar»— necesita que se pueda pulsar el segundo. Eliges el botón eligiendo su token.
`form_submit` exige ese token, es de un solo uso, caduca, y antes de pulsar vuelve a leer el formulario
y recalcula la huella de todo lo rellenado: si algo cambió, da `TokenStale` y no pulsa. Así que lo que
se envía es demostrablemente lo que se mostró.

- **Un botón clasificado `irreversible` necesita el visto bueno explícito del usuario**, pedido antes de
  llamar a `form_submit`, citando el nombre del botón y lo que se va a mandar. Por defecto todo botón de
  envío es irreversible; solo es `draft` si su nombre es de borrador, no es el único botón de envío y no
  apunta a otra ruta; es `step` si es un `Siguiente` con más pasos detrás.
- Fuerzan `irreversible` sin importar el texto: un `confirm()`, un `beforeunload`, una `action` a otro
  origen, un `method=post` a un origen de pago, o las palabras *pagar*, *comprar*, *confirmar pedido* o
  *aceptar y firmar* en el botón o a su alrededor.
- **El usuario siempre puede tomar la ventana del agente y pulsar el botón él mismo**: se la pasas con
  `browser_handoff(reason)`, o la trae él con `Meta+Shift+B` (un atajo que Diego asigna una vez en
  Preferencias > Atajos tras `panic/manos-summon --install`; sin asignar no hace nada). Es la salida buena
  cuando hay cualquier duda, y conviene ofrecerla. Mientras la tenga, todo da `UserDriving`: espera a que diga
  que ha terminado y entonces `browser_resume`.
- Después de enviar, lo que de verdad dice que funcionó es, por orden: el estado HTTP de la propia
  petición del formulario, la navegación con su cadena de redirecciones, que el formulario ya no esté, y
  un mensaje nuevo en un `role=status`/`aria-live`. **Si nada de eso saltó, el resultado es `unknown`,
  no `success`.** Un «enviado» falso y seguro es el fallo más caro de todo el sistema. Y captura siempre
  el identificador de confirmación: es lo que el usuario necesita después.

## Qué valor se ve y cuál no

Esto es una regla del servidor, no una preferencia, y conviene saberla porque explica lo que ves:

- **`browser_observe` no emite nunca el valor de un campo de texto libre**: emite su longitud
  (`len=6`), los reconozca el clasificador o no, y la captura tapa **todos** esos campos. Así que no
  uses `browser_observe` para comprobar lo que acabas de escribir: no está ahí y no va a estar.
- **La relectura de `form_fill` es el único sitio legítimo donde un valor vuelve**, porque el servidor
  sabe lo que acaba de escribir. Ahí llega renderizado como lo leería una persona: la fecha como
  `25/12/1990`, un `select` por el **texto** de la opción y no por su valor, y una marca en cada campo
  que el agente **no** rellenó.
- **Salvo en un campo marcado como secreto.** De una contraseña, un número de tarjeta, un CVC o un
  código de un solo uso la relectura devuelve la longitud y la validez, **nunca el contenido**, y el
  servidor memoriza ese valor para tachar sus ecos en cualquier otra respuesta.
- **Y salvo en un campo que rellenó el servidor desde el perfil**, lo marque el clasificador o no. Ahí
  la relectura devuelve la longitud, la validez y la clave que mandaste (`sent`), nunca el valor: tú
  escribiste `{{perfil.iban}}`, así que el IBAN es un dato que **no tienes**, y enseñártelo no te
  confirma nada —lo que te confirma que entró bien es el `verdict` y la validez—. La diferencia con el
  punto de arriba es de quién sabía el valor, no de qué campo es: la excepción de la relectura existe
  porque **tú** escribiste el valor. Si el usuario quiere verlo, que mire la página en la ventana del
  agente (`browser_handoff`, o `Meta+Shift+B`).
- **Tú planificas con claves y el servidor rellena con valores.** Los datos del usuario se referencian
  como `"{{perfil.clave}}"` (`{{perfil.nombre}}`, `{{perfil.direccion.cp}}`, …) y se resuelven en el
  servidor, en el borde de CDP. Lee `profile.example.toml` para las claves que hay. El fichero lo
  escribe y lo controla el usuario; **no se raspa jamás de las contraseñas guardadas ni del autofill de
  su navegador**.
- **Nunca inventes un valor para un campo obligatorio.** Ni un motivo, ni una fecha plausible, ni un
  número de expediente. Se informa `needs_value` con las restricciones del campo (`pattern`, `min/max`,
  `maxlength`, las opciones si es un `select`) y se le pregunta al usuario. Un formulario con un dato
  inventado es peor que un formulario a medias.

## Contraseñas, tarjetas, CVC y OTP

No se guardan nunca y no se teclean nunca. **El agente rellena todo lo de alrededor y para.** Una
compra o una transferencia es por tanto **siempre una operación a dos**: el agente deja el formulario
listo, se la pasa al usuario con `browser_handoff(reason)` (o él la toma con `Meta+Shift+B`), y el usuario
pone el secreto y pulsa. Después, `browser_resume` cuando él lo diga.

Una cosa que el usuario tiene derecho a saber, y que conviene decirle si pregunta: **enviar un
formulario deja en el perfil del navegador del agente el historial de autocompletado por campo** (una
fila por campo de texto no vacío, en `Default/Web Data`). Es el precio de tener encendido el parseador
de formularios de Chrome, que es lo que hace funcionar el peldaño 1. Las contraseñas y las tarjetas no
entran; un OTP tecleado en un `type=text` sí. Es local, no va a sync, y se revoca borrando el
directorio del perfil.

Esto incluye los campos de pago dentro de un iframe. Mecánicamente se pueden rellenar; no se hace. Se
detectan por el origen (`js.stripe.com`, `checkoutshopper*.adyen.com`, `pay.google.com`, `*.paypal.com`,
`*.redsys.es`) o por contener un campo cuyo `autocomplete` empieza por `cc-`, y el resultado es un
traspaso, no un relleno. Lo mismo con un SMS, un TOTP o un «confírmalo en la app»: el código tiene que
venir del usuario en ese momento, nunca de un almacén.

## Captchas y «no soy un robot»

**No se resuelven, no se esquivan y no se subcontratan. Ni como función, ni como respaldo.** Uno de cada
seis sitios lleva uno, así que esto va a pasar a menudo y es una salida diseñada, no un error.

Marcadores de detección, para parar rápido en vez de dar vueltas: un iframe cuyo `src` empareje
`google.com/recaptcha`, `hcaptcha.com`, `challenges.cloudflare.com`, `*.arkoselabs.com`, `funcaptcha` o
`geetest`; un elemento con `class` que contenga `g-recaptcha`, `h-captcha` o `cf-turnstile`; un
`input[name="cf-turnstile-response"]` o un `textarea[name="g-recaptcha-response"]`; un `role=checkbox`
llamado «No soy un robot» / «I'm not a robot»; o un 403 con cabecera `cf-mitigated`.

Y **no pulses la casilla**: un clic en un reCAPTCHA v2 desde un perfil automatizado suele escalar a un
reto de imágenes y quema el intento. Lo correcto: parar, decirle al usuario qué falta y qué hay ya
relleno, que lo resuelva él en la ventana del agente, y reanudar volviendo a describir el formulario
para comprobar que no se perdió nada.

## Lo que seguirá fallando y necesita a una persona

Por orden de cuántas veces va a pasar de verdad:

1. **Captcha o Turnstile.** ~17 % de los sitios. Por diseño nunca se intenta. Es la razón número uno de
   que una tarea de formulario no acabe sola.
2. **Contraseñas, tarjetas, CVC, OTP y segundos factores del banco.** Por diseño. Operación a dos.
3. **Muros de login y caducidad de sesión a media faena.** Se detecta (navegación a un origen
   inesperado, un 401/403 en el envío) y no se arregla sin credenciales: se para y se dice.
4. **Valores que son una decisión y no un dato**: el motivo de una solicitud, una justificación libre,
   elegir entre dos planes. `needs_value`, nunca invención.
5. **`patternMismatch` contra un `pattern` indocumentado** (típico en DNI, NIE, IBAN y matrículas). Se
   puede decir exactamente qué objeta el navegador, y a veces repararlo, pero un número de documento
   realmente equivocado lo arregla una persona.
6. **Shadow roots cerrados y formularios pintados en canvas.** No hay asideros ni etiquetas: solo
   píxeles, que es justo el régimen donde los agentes miden por debajo del 5 %. Se informa
   `unverifiable` y se traspasa.
7. **Zonas de subida que solo aceptan arrastrar y soltar** y no esconden ningún `input[type=file]`.
   Primero búscalo en el subárbol de la zona, incluidos los shadow roots abiertos; si de verdad no hay,
   traspaso.
8. **Máscaras que rechazan del todo la entrada programática.** Tres intentos y parada.
9. **Antiautomatización por la *forma* de la interacción** (cero movimiento de ratón, teclas
   perfectamente regulares). Mitigable a medias; contra un sitio decidido, no se gana y no merece la
   pena ganarlo.
10. **Asistentes con tokens de paso que caducan en el servidor.** El agente es rápido, pero una pausa
    humana (captcha, OTP) en medio puede invalidar el flujo entero: al reanudar se vuelve a describir y
    a verificar desde el paso uno, nunca se asume.
