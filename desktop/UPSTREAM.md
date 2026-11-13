# Código copiado de kwin-mcp

- Origen: https://github.com/isac322/kwin-mcp
- Commit: `427798d8166e1e051055787b07fab531f17f5dac`
- Licencia: MIT, texto íntegro en `LICENSE.kwin-mcp`.

## Rangos copiados

| fichero local | fichero:líneas upstream | cambios |
|---|---|---|
| `kwin.py` `activate()` | `geometry.py:137-160` | buscar por uuid con `_FIND`; respuesta por `REPLY` en vez de `callDBus`; comprueba la activación en el mismo script |
| `desktop/eis.py` constantes | `input.py:304-341` | solo las capacidades y eventos usados; `_EV_MODIFIERS = 9` añadido; nombres acortados |
| `desktop/eis.py` `_lib()` | `input.py:406-534` | sin táctil, puntero relativo ni scroll; tabla en vez de asignaciones; + keymap, modificadores y grupo |
| `desktop/eis.py` `_setup`…`sync` | `input.py:578-748` | `_setup` recibe el fd (el D-Bus vive en el guardián de `kwin.eis_open`); dispositivo quitado o en pausa → `InputUnavailable` en vez de esperar sustituto; evento MODIFIERS guardado |
| `desktop/eis.py` `_drain` | `input.py:783-808` | igual |
| `desktop/eis.py` `_handle` (asiento, dispositivo), envío | `input.py:858-945` | solo teclado, puntero absoluto y botones |
| `desktop/eis.py` `close` | `input.py:994-1041` | sin táctil; el `disconnect(cookie)` lo hace el guardián |
| `desktop/capture.py` `_RAW` | `screenshot.py:105-115` | solo la tabla de formatos |
| `desktop/capture.py` `_screenshot2` | `screenshot.py:1284-1395` | llamar y luego leer (KWin contesta antes de escribir el pipe): sin hilo lector; la llamada D-Bus vive en `kwin.capture_workspace`; `frombuffer` |
| `desktop/capture.py` `_frame` (ancla) | `screenshot.py:465-487` | la regla del ancla como comprobación: escala 1 y origen (0,0), si no `CaptureFailed` |
| `desktop/capture.py` `_spectacle` | `screenshot.py:1760-1821` | sin puntero ni entorno propio (hereda el del proceso); fichero en un directorio temporal que se borra |
| `desktop/atspi.py` `_spawn` | `core.py:504-550` | `/usr/bin/python3 -I atspi_worker.py --generation N`; sin process_registry; dirección del bus de `healthy()` en `AT_SPI_BUS_ADDRESS`; chequeo de salud antes de cada arranque |
| `desktop/atspi.py` `_read` | `core.py:552-592` | un `select` con plazo de 5 s, sin latido; la escritura vive en `_call` (las acciones no se reintentan) |
| `desktop/atspi_worker.py` `_state_nicks` | `accessibility.py:822-830` | sin docstring |
| `desktop/atspi_worker.py` `_toplevel` | `accessibility.py:1060-1089` | al revés: de ventana KWin a toplevel AT-SPI (pid + título + tamaño de cliente ±1 px); 0 → `NoToplevel`, >1 → `AmbiguousWindow` |
| `desktop/atspi_worker.py` `_caption_consistent` | `accessibility.py:1107-1123` | literal |
| `desktop/atspi_worker.py` `_window_rect` | `accessibility.py:1125-1134` | literal |
| `desktop/atspi_worker.py` `_row` | `accessibility.py:1137-1221` | solo acciones y Text; texto editable/contraseña como `len=N`; sin Value, descripción ni profundidad |
| `desktop/atspi_worker.py` `main` (bucle) | `accessibility.py:1266-1291` | sin argparse ni `_RefTable`; errores tipados `{error, msg}`; bombea el contexto GLib antes de cada petición |

## Comparar un rango con upstream

```sh
git -C <clon> show 427798d8166e1e051055787b07fab531f17f5dac:src/kwin_mcp/input.py | sed -n 'A,Bp' | diff - <(sed -n 'C,Dp' desktop/eis.py)
```
