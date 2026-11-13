"""¿Está la nota escrita en KWrite y guardada tal cual?  (bench/tasks/kwrite-nota-07.json)

Lo que miraría una persona: abre `nota.txt` y lee exactamente las dos líneas dictadas, con su tilde, la hora
y los puntos, sin nada más. KWrite añade un salto de línea al final (medido), y eso no cuenta. Con auditoría,
además, que el texto entró por KWrite y se guardó desde KWrite, no escribiendo el fichero por fuera.
"""

from comun import HOME, auditoria

TEXTO = "Reunión el jueves a las 10:30.\nTraer el informe de ventas."

nota = HOME / "nota.txt"
assert nota.is_file(), "no hay nota.txt en el directorio de trabajo"
leido = nota.read_text(encoding="utf-8")
assert leido.removesuffix("\n") == TEXTO, f"nota.txt dice {leido!r} en vez de {TEXTO!r}"

pasos = auditoria()
if pasos is not None:
    en_kwrite = [p for p in pasos if p["ok"] and p["resource_class"] in ("org.kde.kwrite", "org.kde.kate")]
    assert any(p["tool"] == "ui_act" and p["action"] in ("set_text", "type") for p in en_kwrite), \
        "el texto no entró por KWrite: el fichero se escribió por otro lado"
    # la ventana de KWrite solo ofrece file_save_all (file_save es del editor y no sale por D-Bus, medido);
    # el botón «Save» de la barra, por AT-SPI, también es guardar desde KWrite
    assert any((p["tool"] == "app_invoke" and (p["action"] or "").startswith("file_save"))
               or (p["tool"] == "ui_act" and p["action"] == "press") for p in en_kwrite), \
        "no se guardó desde KWrite"

print("kwrite-nota-07: OK ·", len(leido), "caracteres")
