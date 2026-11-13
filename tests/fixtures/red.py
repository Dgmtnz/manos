"""Una ventana GTK3 de 400x300 pintada de rojo puro, para la captura anidada (N7). Con /usr/bin/python3:
el venv no trae gi."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

w = Gtk.Window(title="rojo")
w.set_default_size(400, 300)
w.set_app_paintable(True)
w.connect("draw", lambda _, cr: (cr.set_source_rgb(1, 0, 0), cr.paint()))
w.connect("destroy", Gtk.main_quit)
w.show_all()
Gtk.main()
