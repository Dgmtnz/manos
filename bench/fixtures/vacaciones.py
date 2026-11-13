"""«Solicitud de vacaciones»: un asistente GTK3 de tres pasos para el bench (asistente-vacaciones-17). Con
/usr/bin/python3 y solo dentro de la KWin anidada.

    XDG_DATA_HOME=... /usr/bin/python3 bench/fixtures/vacaciones.py

Paso 1: nombre y departamento (Ventas marcado de serie). Paso 2: primer y último día, que solo admite
DD/MM/AAAA: otra forma (2026-03-16, 16/3/2026) deja una etiqueta de error y no avanza. Paso 3: el resumen con
los días laborables, que descuenta fines de semana y festivos (el 19/03 es festivo), y «Enviar solicitud».
Cada paso se construye al entrar: los controles de los otros pasos no existen, tampoco en AT-SPI.

Se apunta a sí mismo en `$XDG_DATA_HOME/bench/vacaciones.json` (fuera del alcance de las tools de ficheros del
agente): pid, paso, cada error mostrado y cada envío. `vacaciones.pid` aparece con la ventana en pantalla.
"""

import datetime
import json
import os
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
DIR.mkdir(parents=True, exist_ok=True)
STATE = DIR / "vacaciones.json"
FESTIVOS = {datetime.date(2026, 3, 19)}
DEPARTAMENTOS = ("Ventas", "Compras", "Sistemas")
GLib.set_prgname("manos-vacaciones")

st = {"pid": os.getpid(), "paso": 1, "errores": [], "envios": []}
datos = {"nombre": "", "departamento": "Ventas", "desde": "", "hasta": ""}


def guardar():
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def fecha(texto):
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", texto):
        return None
    try:
        return datetime.datetime.strptime(texto, "%d/%m/%Y").date()
    except ValueError:
        return None


def laborables(a, b):
    dias = (a + datetime.timedelta(n) for n in range((b - a).days + 1))
    return sum(d.weekday() < 5 and d not in FESTIVOS for d in dias)


win = Gtk.Window(title="Solicitud de vacaciones")
win.set_default_size(460, 300)
caja = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
win.add(caja)


def campo(grid, fila, texto, valor):
    entrada = Gtk.Entry(hexpand=True, text=valor)
    entrada.get_accessible().set_name(texto)  # la etiqueta al lado no le da nombre en AT-SPI
    grid.attach(Gtk.Label(label=texto, xalign=0), 0, fila, 1, 1)
    grid.attach(entrada, 1, fila, 1, 1)
    return entrada


def boton(fila, texto, fn):
    b = Gtk.Button(label=texto)
    b.connect("clicked", fn)
    fila.add(b)


def pagina(n, error=""):
    for hijo in caja.get_children():
        hijo.destroy()
    st["paso"] = n
    caja.add(Gtk.Label(label=f"Paso {n} de 3", xalign=0))
    grid, botones = Gtk.Grid(row_spacing=8, column_spacing=8), Gtk.Box(spacing=8)
    caja.add(grid)
    if n == 1:
        nombre = campo(grid, 0, "Nombre completo", datos["nombre"])
        grid.attach(Gtk.Label(label="Departamento", xalign=0), 0, 1, 1, 1)
        radios, grupo = Gtk.Box(spacing=12), None
        for d in DEPARTAMENTOS:
            grupo = Gtk.RadioButton(label=d, group=grupo)
            grupo.set_active(d == datos["departamento"])
            radios.add(grupo)
        grid.attach(radios, 1, 1, 1, 1)

        def siguiente(_):
            datos["nombre"] = nombre.get_text().strip()
            datos["departamento"] = next(r.get_label() for r in radios.get_children() if r.get_active())
            pagina(2) if datos["nombre"] else pagina(1, "Falta el nombre.")
        boton(botones, "Siguiente", siguiente)
    elif n == 2:
        desde, hasta = campo(grid, 0, "Primer día", datos["desde"]), campo(grid, 1, "Último día", datos["hasta"])

        def leer():
            datos["desde"], datos["hasta"] = desde.get_text().strip(), hasta.get_text().strip()

        def atras(_):
            leer()
            pagina(1)

        def siguiente(_):
            leer()
            a, b = fecha(datos["desde"]), fecha(datos["hasta"])
            if a is None or b is None:
                pagina(2, "Fecha no válida: escribe las fechas como DD/MM/AAAA.")
            elif b < a:
                pagina(2, "El último día es anterior al primero.")
            else:
                pagina(3)
        boton(botones, "Atrás", atras)
        boton(botones, "Siguiente", siguiente)
    elif n == 3:
        dias = laborables(fecha(datos["desde"]), fecha(datos["hasta"]))
        caja.add(Gtk.Label(label=f"Resumen: {datos['nombre']}, {datos['departamento']}, del {datos['desde']} "
                                 f"al {datos['hasta']}.", xalign=0))
        caja.add(Gtk.Label(label=f"Días laborables: {dias}", xalign=0))

        def enviar(_):
            st["envios"].append({**datos, "dias": dias})
            pagina(4)
        boton(botones, "Atrás", lambda _: pagina(2))
        boton(botones, "Enviar solicitud", enviar)
    else:
        caja.add(Gtk.Label(label="Solicitud enviada.", xalign=0))
    if error:
        st["errores"].append(error)
        caja.add(Gtk.Label(label=error, xalign=0))
    caja.add(botones)
    caja.show_all()
    guardar()


pagina(1)
win.connect("map-event", lambda *_: (DIR / "vacaciones.pid").write_text(str(os.getpid())))
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
