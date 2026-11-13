"""Una ventana GTK3 vacía cuyo resourceClass es argv[1]. Con /usr/bin/python3 (el venv no trae gi), solo en la
KWin anidada: tests/test_summon_nest.py la usa como navegador falso (ManosBrowser)."""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

GLib.set_prgname(sys.argv[1])  # el app_id de Wayland, y por tanto el resourceClass en KWin
win = Gtk.Window(title=sys.argv[1])
win.set_default_size(600, 400)
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
