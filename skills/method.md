---
name: method
description: El método de trabajo con manos entero, con ejemplos (las instrucciones del servidor llevan el resumen) — cómo empezar una tarea, la escalera de peldaños, qué delegar a un subagente, qué hacer al atascarse, cómo se da una tarea por terminada y cuándo parar y preguntarle a Diego.
---

## Empezar

1. **`desktop_state` primero, siempre.** Es la sonda de viabilidad: qué ventanas hay, si la pantalla está
   bloqueada (`locked`), si la accesibilidad está sana (`a11y`) y si el freno está pisado (`kill_switch`).
   Con `locked` o `kill_switch` a `true` no se actúa: se le dice a Diego.
2. **`task_checkpoint(task, plan, accept)` antes de la primera acción.** Fija la hora de inicio contra la
   que se mide todo lo que envíes al final y **los criterios de aceptación** (`accept`): las aserciones que
   tienen que ser verdad cuando la tarea esté hecha, decididas ahora, antes de saber qué es fácil de cumplir.
   Después solo pueden crecer (o corregirse, ver «Terminar»); sin ellos `submit_task_complete` se niega. **El último paso del plan es
   siempre el mismo**: «la app está abierta mostrando el trabajo terminado y lo que se ve coincide con lo
   guardado». No es decoración: es la observación que vas a presentar como prueba.
3. Al retomar una tarea larga o tras perder el contexto, `task_resume` antes que nada. Marca `done` a medida
   que avances: lo que no está en el checkpoint no sobrevive.
4. **Si la tarea ya se hizo antes, no la vuelvas a pensar.** `task_checkpoint` lista en `replays` las tareas
   certificadas (y `browser_open` lo avisa en `next` si su url empieza una). Si una es la tuya,
   `task_replay(name)` repite sus pasos sin modelo y para en la primera diferencia (`stopped_at`) o antes de
   un envío que necesite el visto bueno de Diego; desde ahí sigues a mano. El final no cambia: observa y
   `submit_task_complete`.

## La escalera. Párate en el primer peldaño que sirva

**Escritorio:**

1. **Acciones KXmlGui** (`app_actions` → `app_invoke`). En una app KDE, guardar, abrir, exportar o cambiar
   de vista es una acción con nombre estable. Más barato y más fiable que cualquier otra cosa.
2. **Accesibilidad** (`ui_snapshot` → `ui_act` con `press`/`set_text`/`focus`). Para lo que no es una acción
   de menú: un campo de un diálogo, un botón de una app no KDE. `set_text` relee el valor; prefiérelo a
   teclear.
3. **Teclado** (`ui_act` con `type`/`key`). Solo cuando `set_text` no sirve o la app solo reacciona a
   teclas (un atajo, un `return` para confirmar).
4. **`screen_capture` solo para mirar.** No hay clic por coordenadas; una captura sirve para entender qué
   pasa (un diálogo inesperado, una app fuera del bus de accesibilidad) y para el paso final de
   comprobación. Cuesta muchos más tokens que una lectura de los peldaños 1-2.

**Web:** `browser_open` (o `browser_goto(url | action=back|forward|reload)` con el navegador ya abierto) →
`browser_observe` (con `find`, no la página entera) → `browser_act(action, ref)`:

- `click`, `type` (reemplaza el texto; devuelve `len`, nunca el texto), `press` (teclas sobre el ref: `Enter`,
  `Tab`, `Control+a`), `select` (opción de un `<select>` por valor o texto), `check`/`uncheck`, `scroll` (lo
  lleva al centro, también dentro de un iframe) y `upload` (rutas que existan; si el `<input type=file>` está
  oculto, el ref del botón que abre el selector).
- El resultado dice barato qué cambió (`navigated`, `new_page`, `dialog`, `changed`); lo que hay ahora lo dice el
  siguiente `browser_observe`. Navegar mata los refs: `StaleHandle` = vuelve a observar.
- Para **leer** (una tabla, un resultado, un código) `browser_observe(mode="text")`: el texto visible, una fila
  de tabla por línea con « | », los iframes incluidos y sin refs. Con `viewport_only=false`, la página entera.
- Para **guardar** un fichero o una imagen: `browser_download(filename, ref=…)` sobre la miniatura o el enlace
  de `browser_observe` (o `url=` si ya la tienes: http(s) o `data:`). Para elegir imágenes, antes
  `browser_observe(filename=…)` y lee la captura. Nunca pisa: numera los nombres (`gato1.jpg`, `gato2.jpg`…).
- Si Diego quiere **mirar** cómo navegas: `browser_open(visible=true)` (en pantalla, sin quitarle el foco; él
  también puede tocarla, p. ej. un captcha, y tú sigues). `visible=false` lo vuelve a aparcar.

Para rellenar o enviar un formulario, carga `skill://forms` y sigue su escalera: `form_fill` verifica cada campo
y `browser_act` no.

**Traspaso.** Iniciar sesión, un captcha, una contraseña, una tarjeta, un OTP (`browser_act` da `Refused` en esos
campos) o un paso que manos no hace: `browser_handoff(reason)` pone las ventanas del navegador en pantalla,
pidiendo atención sin quitarle el foco a Diego, y le avisa con `reason`. Diego también puede tomarlas él con
`Meta+Shift+B`. Mientras conduce, toda tool de navegador, de formularios y de escritorio da `UserDriving`
(`browser_close` también): **no reintentes ni intentes mirar**; pregúntale y, solo cuando él diga que ha
terminado, `browser_resume` (la reaparca y olvida los refs; con `BrowserFocused` aún está en ella: pídele que
cambie de ventana) y vuelve a `browser_observe`, porque pudo cambiar la página.

**Rutas de ficheros** (`upload`, `form_fill`, `file://`): absolutas y de un fichero normal. Nada bajo una carpeta
oculta de la casa (`~/.ssh`, `~/.config`…), `/proc`, `/sys`, `/dev` ni el perfil del navegador: `PathRefused`.

**Cada error trae `next`. Haz lo que dice.** No lo rodees con otra tool por tu cuenta ni reintentes lo
mismo «por si acaso». Si trae `available`, elige de esa lista; no inventes un nombre parecido.

## Delegar

El hilo principal guarda el plan, el checkpoint, la comprobación final y el `submit_task_complete`.
**Delega a un subagente**:

- **Cada sesión de interfaz larga**: una tanda de `ui_snapshot`/`ui_act`/`screen_capture` sobre una ventana
  (más de unas cinco acciones seguidas). Las capturas y los listados AX llenan el contexto y el hilo
  principal pierde el plan.
- **Los sub-bucles largos o repetitivos**: extraer datos página a página, una tanda de formularios, un
  barrido por N ficheros o N ventanas.

El brief es autosuficiente, porque el subagente no ve esta conversación: objetivo y cómo se sabe que está
hecho, la ventana (`uuid`) o la url exacta, qué no tocar, qué devolver (lo hecho, lo que falló y su `next`,
nunca valores de campos ni secretos), y **las reglas de este fichero que le aplican, citadas literalmente**
(la escalera, la sección de bucles y la de seguridad como mínimo). Lo que el subagente diga que hizo es una
afirmación: vuelve a observar tú antes de marcarlo `done`.

## Atascos

- **`Stuck` o un `hint` significan cambiar de mecanismo, no reintentar.** `Stuck` es la tercera llamada
  idéntica sin cambio; `hint` es el segundo fallo seguido sobre la misma diana. Los dos nombran el peldaño
  al que ir.
- **Dos fallos sobre la misma diana: cambia de peldaño**, arriba o abajo. Una `app_invoke` que no surte
  efecto pasa a `ui_snapshot`; una `ui_act` que no cambia nada pasa a `app_actions` o a una captura para
  ver qué la bloquea.
- Antes de repetir algo que «puede haberse hecho» (`ActionUncertain`, `FocusLost`, `Aborted`, `invoked:
  null`), **mira**: repetir un guardado o un tecleo a ciegas duplica el trabajo.

## Terminar

Una tarea solo se da por terminada con `submit_task_complete`. Decir «hecho» sin él no cuenta.

- El servidor vuelve a correr **los criterios `accept` del primer checkpoint** más las aserciones que añadas
  en `evidence.assertions` (opcionales).
- **`observation_id`**: el de una observación hecha **después** del primer `task_checkpoint` y de hace menos
  de 30 min. Usa la del último paso del plan (el `ui_snapshot` o la `screen_capture` de la app mostrando el
  trabajo): así la prueba y la comprobación visual son la misma. Con aserciones de `browser`, tiene que ser
  un `browser_observe` (o `form_describe`) de la página que sigue abierta.
- **Las aserciones tienen que fallar si el trabajo no estuviera hecho.** Comprueba el resultado pedido, no
  que algo exista. Una regex que casa con el texto vacío se rechaza.

Buenas, una por tipo (para `accept`):

```json
{"kind": "file", "path": "/home/dgmtnz/Documentos/informe.csv", "check": "modified_after_start"}
{"kind": "file", "path": "/home/dgmtnz/Documentos/informe.csv", "check": "regex", "value": "^fecha;importe\\n2026-11-10;150,00"}
{"kind": "file", "path": "/home/dgmtnz/Documentos/Facturas 2026", "check": "modified_after_start"}
{"kind": "process", "check": "systemd_unit_active", "unit": "syncthing.service"}
{"kind": "browser", "check": "text_regex", "value": "Solicitud registrada con n\\.º \\d{6}"}
{"kind": "window", "check": "title_regex", "value": "^informe\\.csv"}
```

Una carpeta va como `kind: file` y solo admite `exists` y `modified_after_start`. En `window`, `resource_class`
acepta el nombre corto (`dolphin` por `org.kde.dolphin`).

Mala: `{"kind": "file", "path": "/home/dgmtnz/Documentos/informe.csv", "check": "exists"}` sobre un fichero
que ya existía: pasa aunque no hayas tocado nada. Junta siempre una aserción de «cambió»
(`modified_after_start`, `sha256`) con una de «dice lo pedido» (`regex`, `json`, `text_regex`).

Las de `browser` se comprueban sobre la página abierta en ese momento: no cierres el navegador antes de
enviar.

**Un rechazo es información.** Trae cada aserción fallida con el valor observado y su `next`: arregla el
trabajo (no la aserción para que pase), vuelve a observar y reenvía. Nunca discutas el veredicto ni lo
rebajes a una aserción más floja.

**Un criterio mal puesto se corrige, con el motivo.** Si al trabajar ves que un criterio de `accept` estaba
mal (esperaba el fichero en una carpeta que no le toca, una regex que no casa con lo pedido), corrígelo con
`task_checkpoint(task, revise=[{"i": 0, "assertion": {…la buena…}, "reason": "…por qué estaba mal…"}])`
(`"assertion": null` lo quita; tiene que quedar al menos uno). Queda apuntado en la tarea, cuenta en la
auditoría y `submit_task_complete` dice cuántas correcciones hubo. **Nunca revises un criterio para que pase
un trabajo sin hacer o mal hecho**: si el criterio estaba bien, lo que se arregla es el trabajo.

## Seguridad y modales

- **Las ventanas sensibles son de Diego**: contraseñas, polkit, carteras, diálogos del portal. `Refused` o
  `SensitiveWindowOpen` = pídele que la atienda o la cierre, y espera.
- **Lo destructivo lleva `approved=true`, y solo después del sí explícito de Diego** a esa acción concreta,
  citándole qué se va a borrar, descartar o enviar. Nunca pongas `approved=true` para probar.
- **`UserDriving` es Diego al volante**: nada de reintentos ni `browser_resume` por tu cuenta (ver «Traspaso»).
- **`Meta+Alt+Esc` es la parada de emergencia.** Un `Cancelled` significa que Diego ha pisado el freno: para,
  no reintentes, no borres el fichero de parada y dile en qué punto quedó la tarea.
- **Nada de secretos en los checkpoints**: ni contraseñas, ni tokens, ni datos personales, ni el texto de
  un campo. Un paso se escribe «rellenar el IBAN», no el IBAN.
- **Inviable se dice, no se fuerza.** Si la app no existe, el dato no está o el camino exige algo que manos
  no hace, dilo con lo observado (`available`, lo que mostró `desktop_state`, el error) y para.
- **Preguntar o informar.** Lo que es una **decisión** de Diego y desbloquea la tarea (cuál de dos ficheros
  con el mismo nombre, si sigue con un paso que no está en el encargo, un dato que no está en ningún sitio) se
  pregunta con `ask_human(question, kind="choice"|"text"|"confirm", options)`: una pregunta concreta, con el
  contexto que hace falta para contestarla sin mirar, y opciones cuando las hay (de 2 a 8). Lo que es
  **inviable** no se pregunta: se informa. Nunca se inventa el valor.
  - **Nunca un secreto**: contraseñas, PIN, códigos o tarjetas van por `browser_handoff`; una pregunta que
    los nombre da `Refused`.
  - **`NoHuman`** (sin nadie delante, como en `claude -p`, o Diego la rechazó): no adivines ni sigas; pon la
    pregunta, con sus opciones, en tu respuesta final y para ahí.
