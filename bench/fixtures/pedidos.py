"""«Avisos de pedidos»: una lista GTK3 de seis pedidos, cada uno con su botón «Avisar», para el bench
(pedidos-reanudar-20). Con /usr/bin/python3 y solo dentro de la KWin anidada.

    XDG_DATA_HOME=... /usr/bin/python3 bench/fixtures/pedidos.py [--sembrar]

`--sembrar` no abre ventana: deja la sesión anterior, cortada a medias. Los avisos de 4711, 4712 y 4714 de hace una
hora, y la tarea `avisos-pedidos` de manos con esos tres pasos hechos y la nota de por qué se saltó el 4713.

Como un programa de avisos de verdad, no enseña qué se avisó ya: el botón sigue igual tras pulsarlo y cada
pulsación manda otro aviso al cliente. Lo que se hizo en una sesión anterior solo consta en el checkpoint de
manos que siembra el setup.

Lleva la cuenta en `$XDG_DATA_HOME/bench/pedidos.json` (fuera del alcance de las tools de ficheros del agente):
pid y cada aviso con su hora. Si el fichero ya existe (los avisos de la sesión anterior, sembrados por el
setup), sigue sumando sobre él. `pedidos.pid` aparece con la ventana en pantalla.
"""

import json
import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

DIR = Path(os.environ["XDG_DATA_HOME"]) / "bench"
DIR.mkdir(parents=True, exist_ok=True)
STATE = DIR / "pedidos.json"
PEDIDOS = [(4711, "Ana Martín", "2 cajas"), (4712, "Bodegas Lur", "1 palé"), (4713, "Jorge Sanz", "1 caja"),
           (4714, "Clínica Arce", "3 cajas"), (4715, "Pilar Ortega", "1 sobre"), (4716, "Café Moka", "2 palés")]
GLib.set_prgname("manos-pedidos")

if "--sembrar" in sys.argv:
    hace = time.time() - 3600
    STATE.write_text(json.dumps({"avisos": [{"pedido": p, "ts": round(hace + i * 60, 3)}
                                            for i, p in enumerate((4711, 4712, 4714))]}), encoding="utf-8")
    plan = [f"avisar del pedido {p}" for p, _, _ in PEDIDOS]
    tarea = {"task": "avisos-pedidos", "started": hace - 60, "status": "open", "accept": [],
             "plan": [*plan, "escribir avisos-sesion.txt con KWrite",
                      "comprobar que lo que se ve coincide con lo guardado"],
             "done": [plan[0], plan[1], plan[3]],
             "notes": [{"t": hace + 100, "note": "4713 saltado: faltaba confirmar la dirección de entrega. "
                                                 "Confirmada después por correo: avisarlo antes de cerrar."}]}
    tareas = DIR.parent / "manos/tasks"
    tareas.mkdir(mode=0o700, parents=True, exist_ok=True)
    (tareas / "avisos-pedidos.json").write_text(json.dumps(tarea, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0)

st = {"avisos": []} | (json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {})
st["pid"] = os.getpid()


def guardar():
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def avisar(_, numero):
    st["avisos"].append({"pedido": numero, "ts": round(time.time(), 3)})
    guardar()


win = Gtk.Window(title="Avisos de pedidos")
win.set_default_size(460, 320)
grid = Gtk.Grid(row_spacing=8, column_spacing=12, margin=12)
win.add(grid)
for fila, (numero, cliente, bultos) in enumerate(PEDIDOS):
    grid.attach(Gtk.Label(label=f"Pedido {numero} · {cliente} · {bultos}", xalign=0, hexpand=True), 0, fila, 1, 1)
    b = Gtk.Button(label="Avisar")
    b.get_accessible().set_name(f"Avisar del pedido {numero}")
    b.connect("clicked", avisar, numero)
    grid.attach(b, 1, fila, 1, 1)

guardar()
win.connect("map-event", lambda *_: (DIR / "pedidos.pid").write_text(str(os.getpid())))
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
