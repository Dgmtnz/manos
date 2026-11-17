# manos · guía de uso

manos es un servidor MCP que da manos a Claude Code en este escritorio (KDE Plasma 6, Wayland): navega webs,
rellena formularios, extrae datos, trabaja con hojas y documentos y maneja apps de escritorio. El razonamiento lo
pone el modelo; manos pone las manos y los frenos: no envía, no paga y no teclea secretos sin ti.

## 1. Puesta en marcha (ya hecha)

| Qué | Cómo comprobarlo |
|---|---|
| Registrado en Claude Code para todos los proyectos | `claude mcp get manos` → `✔ Connected` |
| `Meta+Shift+B`: tomar el navegador del agente | Preferencias del sistema → Atajos → «Tomar el navegador de manos» |
| `Meta+Alt+Esc`: parada de emergencia | Preferencias del sistema → Atajos → «Parada de emergencia de manos» |
| Todo funciona | `uv run --project . server.py --selftest` → `todo OK` (~10 min) |

## 2. Cómo se usa

1. Abre una sesión desde la carpeta donde quieras los ficheros: `cd ~/Documents/algo && claude --model sonnet`.
2. Pide la tarea en una frase, como a una persona. No hace falta nombrar las tools.
3. El agente planifica, actúa y, antes de decir «hecho», el servidor comprueba el resultado
   (`submit_task_complete`): si no lo puede demostrar, no te dice que está hecho.
4. Si necesita algo tuyo (un login, un captcha, una decisión), para y te lo pide.

Sonnet es el modelo recomendado: 97 % en el banco de 35 tareas y 7/8 en la prueba real. Haiku sirve para cosas
sencillas, pero se equivoca bastante más.

Ejemplos de la prueba real (`docs/prueba-real.md`):

- «Mira en aemet.es la previsión de mañana para Murcia y dime máxima, mínima y probabilidad de lluvia.»
- «Lee ~/Downloads/volumen_entrenamiento.xlsx, suma las series por músculo y guárdalo en resumen.xlsx.»
- «Convierte ~/Downloads/DT_template.docx a PDF y dime cuántas páginas tiene.»
- «En Dolphin, crea la carpeta pdf-noviembre en Downloads y mueve ahí los PDF de este mes.»
- «Saca de la Wikipedia la clasificación de Primera y guárdala en tabla.xlsx con puntos por partido.»

## 3. Qué puede hacer

| Área | Qué hace | Tools |
|---|---|---|
| Web | Abre, navega, lee la página por elementos (no por píxeles), hace clic, escribe, responde diálogos, sube ficheros, entra en iframes | `browser_open`, `browser_observe`, `browser_act`, `browser_goto`, `browser_dialog` |
| Modo mirar | Pídele «quiero ver el navegador»: la ventana sale en pantalla sin quitarte el foco y puedes tocarla a la vez (p. ej. resolver un captcha) mientras el agente sigue | `browser_open(visible=true)` |
| Formularios | Rellena un formulario entero de una vez (fechas, máscaras, selects, asistentes por pasos, shadow DOM), lo relee y lo valida; enviar es un paso aparte | `form_describe`, `form_fill`, `form_submit` |
| Extracción | Tablas paginadas o APIs JSON a `.csv`/`.ndjson`, sin pasar los datos por la conversación; reanudable, respeta los límites de peticiones del sitio | `browser_extract` |
| Descargas | Guarda un fichero o una imagen de una url (también las `data:` de Google Imágenes) sin pisar nada | `browser_download` |
| Herramientas de la página | Si una web declara sus propias herramientas (WebMCP), las usa directamente | `browser_page_tool` |
| Cookies | Rechaza los banners solo; nunca acepta por ti | (automático) |
| Ofimática | Lee, escribe (con fórmulas) y convierte .ods/.xlsx/.csv/.odt/.docx/.pdf sin abrir ninguna ventana | `office_read`, `office_write`, `office_convert` |
| Escritorio | Ve las ventanas, lanza apps, usa los menús de las apps KDE por su nombre, maneja cualquier app accesible (GTK incluidas) y, como último recurso, captura y teclado | `desktop_state`, `app_launch`, `app_actions`, `app_invoke`, `ui_snapshot`, `ui_act`, `window_set`, `screen_capture` |
| Tareas largas | Plan con puntos de control, reanudar otro día por su nombre, criterios de aceptación que el servidor comprueba | `task_checkpoint`, `task_resume`, `submit_task_complete` |
| Repetición | Una tarea ya certificada se repite sin pensar sus pasos (más barata); nunca repite un envío | `task_replay` |
| Memoria | Tras cada tarea certificada apunta qué funcionó en cada app; la siguiente lo lee primero | `skill://memory/<app>` |
| Preguntar | Te pregunta lo que solo tú decides | `ask_human` |

## 4. Lo que no hace sin ti

- **Contraseñas, PIN, códigos, tarjetas, captchas, Cloudflare:** te pasa el navegador (`browser_handoff`). La
  ventana aparece en pantalla; haces lo tuyo y le dices «sigue». Los logins quedan en su perfil, así que basta
  hacerlo una vez.
- **Enviar, pagar, borrar, confirmar compras:** pide aprobación. Ojo: si en la tarea dices «envíalo», lo toma
  como tu sí. Si quieres revisar antes, di «no lo envíes, para antes».
- **Ventanas sensibles** (gestor de contraseñas y similares): no las lee ni las recuerda.
- **Una petición de verificación anti-bot** no se la salta nunca: te la pasa.

## 5. Tus mandos

| Quieres… | Haz |
|---|---|
| Ver el navegador y echar una mano | Pídele que te lo enseñe (modo mirar): lo ves y lo tocas mientras él sigue |
| Quitarle el navegador del todo | `Meta+Shift+B` (lo trae y el agente se queda quieto hasta que le digas «sigue») |
| Pararlo todo ya | `Meta+Alt+Esc`: congela navegador y acciones (reversible) |
| Reanudar tras la parada | `panic/manos-panic --resume` |
| Matar lo congelado | El comando que muestra la notificación de la parada |
| Que siga tras un traspaso | Dile «ya está, sigue» en la sesión |

El navegador del agente vive fuera de pantalla a propósito, para no taparte el trabajo; solo aparece en un
traspaso o en modo mirar (control compartido). Se cierra con la sesión de `claude` que lo abrió: un traspaso solo dura lo que esa sesión.

## 6. Dónde queda todo

`~/.local/share/manos/`:

| Fichero | Qué es |
|---|---|
| `browser-profile/` | El perfil del navegador del agente: cookies y logins |
| `audit.jsonl` | Una línea por acción: tool, peldaño, ok/error. Sin textos, valores ni urls |
| `memory/<app>.md` | Lo aprendido por app |
| `replay/`, `tasks/` | Tareas certificadas para repetir y tareas en curso |
| `stop`, `driving` | Parada de emergencia y traspaso activos (los gestionan los atajos) |

## 7. Mantenimiento

- Comprobar: `uv run --project . server.py --selftest`.
- Banco (35 tareas, ~$3 con Sonnet): `flock "$XDG_RUNTIME_DIR/manos-bench.lock" uv run --project . bench/run.py --core --model sonnet`.
- El navegador es el Brave instalado (`/opt/brave-bin/brave`, `CHROME` en `browser/session.py`) con un perfil
  propio del agente, no el tuyo. Si actualizas Brave o Playwright, repite el selftest.
- No lances dos selftests o bancos a la vez: comparten perfil y puerto (por eso el `flock`).

## 8. Consejos

- Una tarea por sesión; las sesiones largas encarecen cada paso.
- Si quieres que lo haga en una app concreta, nómbrala («en Dolphin…»); si no, irá por el camino más directo.
- Di dónde guardar el resultado y con qué nombre.
- Para cosas que repites (un informe semanal), usa siempre el mismo nombre de tarea: la segunda vez sale más barata.
