"""«Alta de socio»: un formulario GTK3 para las tareas de escritorio del bench. Con /usr/bin/python3 (el venv
no trae gi), y solo dentro de la KWin anidada.

    BENCH_HOME=... /usr/bin/python3 bench/fixtures/alta.py [--aviso] [--muere]

Igual que el testigo de serve.py, se instrumenta a sí mismo: en cada cambio reescribe `$BENCH_HOME/alta.json`
con su pid, el tamaño de su ventana, lo que hay en cada campo, cada envío y el estado del aviso. El
verificador corre después, quizá con la anidada ya muerta, y solo lee ese fichero. `alta.pid` aparece cuando la
ventana ya está en pantalla, y es lo que mata el teardown.

    --aviso   300 ms después del primer cambio abre un aviso modal. Mientras siga abierto, «Enviar» no
              envía: AT-SPI se salta el bloqueo de un modal, así que lo hace cumplir el propio formulario.
    --muere   la primera instancia se cae 1 s después del primer cambio y deja `alta.murio`; las que se
              lancen después ya no se caen.
"""

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

HOME = Path(os.environ["BENCH_HOME"])
STATE, MURIO = HOME / "alta.json", HOME / "alta.murio"
GLib.set_prgname("manos-alta")  # el app_id de Wayland, y por tanto el resourceClass en KWin

st = {"pid": os.getpid(), "size": [0, 0], "fields": {}, "envios": [], "aviso": None, "rechazados": 0}
tocado = False


def guardar(*_):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


win = Gtk.Window(title="Alta de socio")
win.set_default_size(420, 260)
grid = Gtk.Grid(row_spacing=8, column_spacing=8, margin=12)
win.add(grid)


def campo(fila, texto):
    etiqueta, entrada = Gtk.Label(label=texto, xalign=0), Gtk.Entry(hexpand=True)
    entrada.get_accessible().set_name(texto)  # la etiqueta al lado no le da nombre en AT-SPI (medido)
    grid.attach(etiqueta, 0, fila, 1, 1)
    grid.attach(entrada, 1, fila, 1, 1)
    return entrada


nombre, correo = campo(0, "Nombre completo"), campo(1, "Correo electrónico")
grid.attach(Gtk.Label(label="Cuota", xalign=0), 0, 2, 1, 1)
mensual = Gtk.RadioButton(label="Mensual")
anual = Gtk.RadioButton(label="Anual", group=mensual)
cuotas = Gtk.Box(spacing=12)
cuotas.add(mensual)
cuotas.add(anual)
grid.attach(cuotas, 1, 2, 1, 1)
reglamento = Gtk.CheckButton(label="Acepto el reglamento")
grid.attach(reglamento, 1, 3, 1, 1)
enviar, estado = Gtk.Button(label="Enviar"), Gtk.Label(label="", xalign=0)
grid.attach(enviar, 1, 4, 1, 1)
grid.attach(estado, 0, 5, 2, 1)


def valores():
    return {"nombre": nombre.get_text(), "correo": correo.get_text(),
            "cuota": "anual" if anual.get_active() else "mensual", "reglamento": reglamento.get_active()}


def cerrar_aviso(dialogo, _respuesta):
    dialogo.destroy()
    st["aviso"] = "cerrado"
    guardar()


def abrir_aviso():
    d = Gtk.Dialog(title="Aviso", transient_for=win, modal=True)
    d.add_button("Entendido", Gtk.ResponseType.OK)
    d.get_content_area().add(Gtk.Label(label="Tu sesión caduca en 5 minutos.", margin=16))
    d.connect("response", cerrar_aviso)
    d.show_all()
    st["aviso"] = "abierto"
    guardar()


def cambio(*_):
    global tocado
    st["fields"] = valores()
    if not tocado:
        tocado = True
        if "--aviso" in sys.argv:
            GLib.timeout_add(300, abrir_aviso)
        if "--muere" in sys.argv and not MURIO.exists():
            MURIO.write_text(str(os.getpid()))
            GLib.timeout_add(1000, os._exit, 3)  # sin limpiar nada, como una caída
    guardar()


def envio(_):
    if st["aviso"] == "abierto":
        st["rechazados"] += 1
        estado.set_text("Hay un aviso abierto: ciérralo antes de enviar.")
    else:
        st["envios"].append(valores())
        estado.set_text("Alta enviada.")
    guardar()


def tamano(*_):
    if [*win.get_size()] != st["size"]:
        st["size"] = [*win.get_size()]
        guardar()


for w in (nombre, correo):
    w.connect("changed", cambio)
for w in (mensual, anual, reglamento):
    w.connect("toggled", cambio)
enviar.connect("clicked", envio)
win.connect("size-allocate", tamano)
win.connect("map-event", lambda *_: (HOME / "alta.pid").write_text(str(os.getpid())))  # el setup espera a esto
win.connect("destroy", Gtk.main_quit)
st["fields"] = valores()
guardar()
win.show_all()
Gtk.main()
