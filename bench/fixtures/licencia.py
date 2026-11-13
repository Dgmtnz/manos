"""«Licencias»: activar una licencia en una app GTK3 lenta y con un aviso que roba el foco, para el bench
(licencia-modal-21). Con /usr/bin/python3 y solo dentro de la KWin anidada.

    XDG_DATA_HOME=... /usr/bin/python3 bench/fixtures/licencia.py

- Lenta: la ventana sale 4 s después de arrancar, y «Activar» tarda 3 s en enseñar el resultado.
- El campo «Código de licencia» no admite pegar: un texto que entra sin pulsaciones de teclado (el set_text
  de AT-SPI) no se inserta, y lo dice una etiqueta. Hay que teclearlo. Vaciarlo sí se puede.
- A la tercera pulsación en ese campo se abre un aviso modal que se queda el foco: lo que se siga tecleando
  ya no llega al campo. Mientras el aviso siga abierto, «Activar» no activa.

Se apunta en `$XDG_DATA_HOME/bench/licencia.json` (fuera del alcance de las tools de ficheros del agente): pid,
estado del aviso, pegados bloqueados, rechazos y cada activación con lo que tenían los campos al pulsar.
`licencia.pid` aparece con la ventana en pantalla.
"""

import json
import os
import time
import zlib
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
DIR.mkdir(parents=True, exist_ok=True)
STATE = DIR / "licencia.json"
GLib.set_prgname("manos-licencia")

st = {"pid": os.getpid(), "aviso": None, "pegados": 0, "rechazados": 0, "activaciones": []}
ultima_tecla, pulsaciones = 0.0, 0


def guardar():
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def equipo(codigo):
    return 10000 + zlib.crc32(codigo.encode()) % 90000


win = Gtk.Window(title="Licencias")
win.set_default_size(460, 220)
grid = Gtk.Grid(row_spacing=8, column_spacing=8, margin=12)
win.add(grid)


def campo(fila, texto):
    e = Gtk.Entry(hexpand=True)
    e.get_accessible().set_name(texto)  # la etiqueta al lado no le da nombre en AT-SPI
    grid.attach(Gtk.Label(label=texto, xalign=0), 0, fila, 1, 1)
    grid.attach(e, 1, fila, 1, 1)
    return e


titular, codigo = campo(0, "Titular"), campo(1, "Código de licencia")
nota = Gtk.Label(label="", xalign=0)
activar = Gtk.Button(label="Activar")
resultado = Gtk.Label(label="", xalign=0)
grid.attach(nota, 1, 2, 1, 1)
grid.attach(activar, 1, 3, 1, 1)
grid.attach(resultado, 0, 4, 2, 1)


def cerrar_aviso(dialogo, _respuesta):
    dialogo.destroy()
    st["aviso"] = "cerrado"
    guardar()


def abrir_aviso():
    d = Gtk.Dialog(title="Actualización disponible", transient_for=win, modal=True)
    d.add_button("Más tarde", Gtk.ResponseType.CLOSE)
    d.get_content_area().add(Gtk.Label(label="Hay una versión nueva de Licencias.", margin=16))
    d.connect("response", cerrar_aviso)
    d.show_all()
    st["aviso"] = "abierto"
    guardar()


def tecla(_w, ev):
    global ultima_tecla, pulsaciones
    ultima_tecla = time.monotonic()
    if ev.string and ev.string.isprintable():
        pulsaciones += 1
        if pulsaciones == 3 and st["aviso"] is None:
            GLib.idle_add(abrir_aviso)
    return False


def insertar(entrada, texto, _n, _pos):
    if time.monotonic() - ultima_tecla > 0.3:  # no viene de una pulsación: pegado o set_text
        entrada.stop_emission_by_name("insert-text")
        st["pegados"] += 1
        nota.set_text("Este campo no admite pegar: escribe el código con el teclado.")
        guardar()


def activa(_):
    if st["aviso"] == "abierto":
        st["rechazados"] += 1
        resultado.set_text("Cierra antes el aviso de actualización.")
        guardar()
        return
    c = codigo.get_text()
    st["activaciones"].append({"titular": titular.get_text(), "codigo": c, "equipo": equipo(c)})
    guardar()
    resultado.set_text("Activando…")
    GLib.timeout_add(3000, lambda: resultado.set_text(f"Licencia activada. Equipo n.º {equipo(c)}.") and False)


codigo.connect("key-press-event", tecla)
codigo.connect("insert-text", insertar)
activar.connect("clicked", activa)
guardar()
win.connect("map-event", lambda *_: (DIR / "licencia.pid").write_text(str(os.getpid())))
win.connect("destroy", Gtk.main_quit)
GLib.timeout_add(4000, lambda: win.show_all() and False)
Gtk.main()
