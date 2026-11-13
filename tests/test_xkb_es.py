"""Autocomprobación de desktop/xkb_es.py, sin compositor:  uv run --project . tests/test_xkb_es.py

Keymap `es` compilado desde nombres (evdev, pc105, es) y pasado por texto, como lo daría KWin. Cada
plan se reproduce en un xkb_state nuevo y por el Compose del locale, igual que haría el cliente.
"""

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.xkb_es import REPERTOIRE, ChordDenied, Keymap, Untypeable, lib

k = Keymap(Keymap.from_names(layout="es").text())
S, G = 42, 100
expected = {"@": [((G,), 3)], "#": [((G,), 4)], "€": [((G,), 18)], "ñ": [((), 39)], "¡": [((), 13)],
            "¿": [((S,), 13)], "á": [((), 40), ((), 30)], "ü": [((S,), 40), ((), 22)], "\n": [((), 28)]}
for ch, strokes in expected.items():
    assert k.plan(ch) == strokes, (ch, k.plan(ch))

# Cada pulsación directa da su carácter en un estado nuevo.
for ch, (mods, code) in k.table().items():
    if not ch.startswith("dead_"):
        assert chr(k.press(mods, code)[0]) == ch, (ch, mods, code)

# Todo REPERTOIRE, muertas incluidas, vuelve igual pasando por Compose (es_ES.UTF-8 usa el de en_US).
x, p = lib(), ctypes.c_void_p
x.xkb_compose_table_new_from_locale.restype, x.xkb_compose_table_new_from_locale.argtypes = p, [p, ctypes.c_char_p, ctypes.c_int]
x.xkb_compose_state_new.restype, x.xkb_compose_state_new.argtypes = p, [p, ctypes.c_int]
x.xkb_compose_state_feed.argtypes = [p, ctypes.c_uint32]
x.xkb_compose_state_get_status.argtypes = x.xkb_compose_state_reset.argtypes = [p]
x.xkb_compose_state_get_utf8.argtypes = [p, ctypes.c_char_p, ctypes.c_size_t]
cs = x.xkb_compose_state_new(x.xkb_compose_table_new_from_locale(x.xkb_context_new(0), b"en_US.UTF-8", 0), 0)
assert cs, "sin tabla Compose"
typed, buf = "", ctypes.create_string_buffer(16)
for mods, code in k.plan(REPERTOIRE):
    cp, sym = k.press(mods, code)
    x.xkb_compose_state_feed(cs, sym)
    match x.xkb_compose_state_get_status(cs):
        case 0: typed += chr(cp)  # NOTHING: la tecla tal cual
        case 2: x.xkb_compose_state_get_utf8(cs, buf, 16); typed += buf.value.decode(); x.xkb_compose_state_reset(cs)
assert typed == REPERTOIRE, (typed, REPERTOIRE)

for bad in ("中", "a中"):
    try:
        k.plan(bad); raise AssertionError(bad)
    except Untypeable as e:
        assert str(e) == "中", e

# Con BloqMayús, libxkbcommon da 'A' sin modificadores y 'a' con Shift.
assert k.plan("a", locked=2) == [((S,), 30)] and k.plan("A", locked=2) == [((), 30)]

assert k.chord("ctrl+c") == ((29,), 46) and k.chord("alt+f4") == ((56,), 62) and k.chord("Return") == ((), 28)
assert k.chord("ctrl+shift+t") == ((29, S), 20)
for c in ("meta+l", "super+x", "ctrl+alt+f2", "ctrl+alt+delete", "Ctrl+Alt+BackSpace"):
    try:
        k.chord(c); raise AssertionError(c)
    except ChordDenied:
        pass

# Una sesión EIS por llamada: crear y soltar keymaps no debe romper ni acumular.
text = k.text()
for _ in range(200):
    Keymap(text).plan("a")
assert Keymap(text).plan("@") == [((G,), 3)]

print("test_xkb_es OK")
