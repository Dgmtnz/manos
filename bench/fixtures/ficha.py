"""«Ficha de cliente 1042»: dos instancias GTK3 de la misma app para el bench (crm-dos-ventanas-19). Con
/usr/bin/python3 y solo dentro de la KWin anidada.

    XDG_DATA_HOME=... /usr/bin/python3 bench/fixtures/ficha.py --antiguo | --nuevo

Las dos ventanas son la misma app (`manos-ficha`): en KWin comparten resourceClass y solo las separa el título
(«… — CRM antiguo» / «… — CRM nuevo»). La antigua es de consulta: campos de solo lectura, que AT-SPI sí deja
leer. La nueva trae datos viejos de una importación (razón social cortada, el teléfono fijo) y valida al
guardar: el teléfono tiene que ir como +34 y nueve cifras seguidas; si no, una etiqueta de error y no guarda.

Cada instancia se apunta en `$XDG_DATA_HOME/bench/ficha-<rol>.json`: pid, los campos tal como están, cada
guardado y cada rechazo. `ficha-<rol>.pid` aparece con la ventana en pantalla.
"""

import json
import os
import re
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

ROL = "antiguo" if "--antiguo" in sys.argv else "nuevo"
DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
DIR.mkdir(parents=True, exist_ok=True)
STATE = DIR / f"ficha-{ROL}.json"
GLib.set_prgname("manos-ficha")

CAMPOS = {
    "antiguo": [("Razón social", "Talleres Ebro Hermanos S.L."), ("NIF", "B50123456"),
                ("Teléfono fijo", "976 123 456"), ("Teléfono móvil", "612 345 678"),
                ("Correo", "compras@talleresebro.example"),
                ("Dirección de facturación", "Calle Mayor 12, 50001 Zaragoza"),
                ("Dirección de envío", "Polígono Malpica, calle E, nave 7, 50016 Zaragoza")],
    "nuevo": [("Razón social", "Talleres Ebro"), ("NIF", "B50123456"), ("Teléfono", "976123456"),
              ("Correo", ""), ("Dirección de envío", "")],
}[ROL]

st = {"pid": os.getpid(), "campos": dict(CAMPOS), "guardados": [], "rechazos": []}


def guardar_estado(*_):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


win = Gtk.Window(title=f"Ficha de cliente 1042 — CRM {ROL}")
win.set_default_size(520, 320)
grid = Gtk.Grid(row_spacing=8, column_spacing=8, margin=12)
win.add(grid)
entradas = {}
for fila, (nombre, valor) in enumerate(CAMPOS):
    e = Gtk.Entry(hexpand=True, text=valor, editable=ROL == "nuevo")
    e.get_accessible().set_name(nombre)  # la etiqueta al lado no le da nombre en AT-SPI
    grid.attach(Gtk.Label(label=nombre, xalign=0), 0, fila, 1, 1)
    grid.attach(e, 1, fila, 1, 1)
    entradas[nombre] = e


def cambio(*_):
    st["campos"] = {k: e.get_text() for k, e in entradas.items()}
    guardar_estado()


for e in entradas.values():
    e.connect("changed", cambio)

if ROL == "nuevo":
    aviso = Gtk.Label(label="", xalign=0)

    def guardar(_):
        campos = {k: e.get_text().strip() for k, e in entradas.items()}
        if not re.fullmatch(r"\+34\d{9}", campos["Teléfono"]):
            st["rechazos"].append("telefono")
            aviso.set_text("Teléfono no válido: escríbelo como +34 seguido de las 9 cifras, sin espacios.")
        elif not all(campos.values()):
            st["rechazos"].append("vacio")
            aviso.set_text("Hay campos vacíos.")
        else:
            st["guardados"].append(campos)
            aviso.set_text("Ficha guardada.")
        guardar_estado()

    b = Gtk.Button(label="Guardar")
    b.connect("clicked", guardar)
    grid.attach(b, 1, len(CAMPOS), 1, 1)
    grid.attach(aviso, 0, len(CAMPOS) + 1, 2, 1)

guardar_estado()
win.connect("map-event", lambda *_: (DIR / f"ficha-{ROL}.pid").write_text(str(os.getpid())))
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
