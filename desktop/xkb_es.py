"""Planificador de pulsaciones: texto → teclas evdev, simulando el keymap XKB que nos da KWin.

Nada de tablas a mano para la distribución: se recorre cada tecla con libxkbcommon bajo
[(), Shift, AltGr, Shift+AltGr] y se queda, por carácter, la pulsación con menos modificadores y
el keycode más bajo. Las teclas muertas las compone el cliente (Compose del locale), no KWin.

Lo comprobado en esta máquina (Compose de en_US.UTF-8, que es también el de es_ES.UTF-8):
  - `dead_acute space` da `'` y `dead_diaeresis space` da `"`, no `´` ni `¨`. La muerta pulsada dos
    veces sí da el acento suelto en los cinco casos, así que es lo que se usa.

Autocomprobación:  uv run --project . tests/test_xkb_es.py
"""

import ctypes
import unicodedata
from functools import cache

REPERTOIRE = "@#€ñÑáéíóúÁüÜ¿?¡!çÇºª¬|\\[]{}~^`´¨'\"<>·" + "".join(map(chr, range(32, 127)))

# de kwin-mcp input.py:102-150 (MIT)
NAMED = {
    "return": 28, "enter": 28, "tab": 15, "escape": 1, "backspace": 14, "delete": 111, "space": 57,
    "up": 103, "down": 108, "left": 105, "right": 106, "home": 102, "end": 107,
    "page_up": 104, "pageup": 104, "page_down": 109, "pagedown": 109, "insert": 110,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "print": 99, "scroll_lock": 70, "pause": 119, "caps_lock": 58, "num_lock": 69, "menu": 127,
}
MODS = {"shift": 42, "ctrl": 29, "control": 29, "alt": 56, "altgr": 100}
ALTGR = 100
# Acento suelto → su muerta; marca combinante (NFD) → su muerta.
SPACING = {"´": "dead_acute", "`": "dead_grave", "^": "dead_circumflex", "¨": "dead_diaeresis", "~": "dead_tilde"}
COMBINING = {"́": "dead_acute", "̀": "dead_grave", "̂": "dead_circumflex",
             "̈": "dead_diaeresis", "̃": "dead_tilde", "̧": "dead_cedilla"}


class InputUnavailable(Exception): pass
class Untypeable(ValueError): pass
class ChordDenied(ValueError): pass


@cache
def lib():
    x = ctypes.CDLL("libxkbcommon.so.0")
    p, u, i = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    for name, res, args in [
        ("xkb_context_new", p, [i]), ("xkb_context_unref", None, [p]), ("xkb_keymap_unref", None, [p]),
        ("xkb_keymap_new_from_string", p, [p, ctypes.c_char_p, i, i]),
        ("xkb_keymap_new_from_names", p, [p, p, i]),
        ("xkb_keymap_get_as_string", p, [p, i]),
        ("xkb_keymap_min_keycode", u, [p]),
        ("xkb_keymap_key_get_syms_by_level", i, [p, u, u, u, ctypes.POINTER(ctypes.POINTER(u))]),
        ("xkb_state_new", p, [p]), ("xkb_state_unref", None, [p]),
        ("xkb_state_update_mask", i, [p, u, u, u, u, u, u]),
        ("xkb_state_update_key", i, [p, u, i]),
        ("xkb_state_key_get_utf32", u, [p, u]), ("xkb_state_key_get_one_sym", u, [p, u]),
        ("xkb_keysym_get_name", i, [u, ctypes.c_char_p, ctypes.c_size_t]),
    ]:
        f = getattr(x, name)
        f.restype, f.argtypes = res, args
    return x


def sym_name(sym):
    buf = ctypes.create_string_buffer(64)
    lib().xkb_keysym_get_name(sym, buf, 64)
    return buf.value.decode()


class Keymap:
    def __init__(self, text=None, _names=None):
        x = lib()
        # Un Keymap por sesión EIS: contexto y keymap se liberan en __del__.
        self.ctx, self.tables = x.xkb_context_new(0), {}
        self.ptr = (x.xkb_keymap_new_from_names(self.ctx, _names, 0) if _names is not None
                    else x.xkb_keymap_new_from_string(self.ctx, text.encode(), 1, 0))  # 1 = TEXT_V1
        if not self.ptr:
            raise InputUnavailable("libxkbcommon no compila el keymap")
        # Solo el bloque del teclado físico (evdev < 128): el keymap evdev también trae teclas como
        # KEY_NUMERIC_POUND (523) o KEY_EURO (435) que darían `#` y `€` sin AltGr.
        self.codes = range(x.xkb_keymap_min_keycode(self.ptr), 128 + 8)
        # <LVL3> (evdev 84) también lleva ISO_Level3_Shift; lo que importa es que la AltGr real sea la 100.
        self.shift = next((c - 8 for c in self.codes if self._level0(c) == "Shift_L"), None)
        if self.shift is None or self._level0(ALTGR + 8) != "ISO_Level3_Shift":
            raise InputUnavailable(f"keymap sin Shift_L o sin ISO_Level3_Shift en evdev {ALTGR}")

    @classmethod
    def from_names(cls, layout="es", model="pc105", rules="evdev"):
        # Para tests y --diag: el mismo keymap que compilaría KWin sin red ni D-Bus.
        return cls(_names=(ctypes.c_char_p * 5)(rules.encode(), model.encode(), layout.encode(), b"", b""))

    def __del__(self):
        x = lib()
        if getattr(self, "ptr", None):
            x.xkb_keymap_unref(self.ptr)
        if getattr(self, "ctx", None):
            x.xkb_context_unref(self.ctx)

    def text(self):
        return ctypes.string_at(lib().xkb_keymap_get_as_string(self.ptr, 1)).decode()

    def _level0(self, kc):
        out = ctypes.POINTER(ctypes.c_uint32)()
        return sym_name(out[0]) if lib().xkb_keymap_key_get_syms_by_level(self.ptr, kc, 0, 0, ctypes.byref(out)) else None

    def press(self, mods, code, locked=0, group=0):
        """Simula una pulsación en un estado nuevo → (utf32, keysym)."""
        x = lib()
        st = x.xkb_state_new(self.ptr)
        try:
            x.xkb_state_update_mask(st, 0, 0, locked, 0, 0, group)
            for m in mods:
                x.xkb_state_update_key(st, m + 8, 1)
            return x.xkb_state_key_get_utf32(st, code + 8), x.xkb_state_key_get_one_sym(st, code + 8)
        finally:
            x.xkb_state_unref(st)

    def table(self, locked=0, group=0):
        """carácter o nombre `dead_*` → (mods, code), la de menos modificadores y keycode más bajo."""
        if (locked, group) in self.tables:
            return self.tables[locked, group]
        out = self.tables[locked, group] = {}
        for mods in [(), (self.shift,), (ALTGR,), (self.shift, ALTGR)]:
            for kc in self.codes:
                cp, sym = self.press(mods, kc - 8, locked, group)
                name = sym_name(sym)
                if name.startswith("KP_"):
                    continue
                if name.startswith("dead_"):
                    out.setdefault(name, (mods, kc - 8))
                elif cp and chr(cp).isprintable():
                    out.setdefault(chr(cp), (mods, kc - 8))
        return out

    def plan(self, text, locked=0, group=0):
        t, strokes, bad = self.table(locked, group), [], []
        for ch in text:
            if ch in "\n\t":
                strokes.append(((), NAMED["return" if ch == "\n" else "tab"]))
            elif ch in t:
                strokes.append(t[ch])
            elif SPACING.get(ch) in t:
                strokes += [t[SPACING[ch]]] * 2
            elif (d := unicodedata.normalize("NFD", ch))[:1] in t and len(d) == 2 and COMBINING.get(d[1]) in t:
                strokes += [t[COMBINING[d[1]]], t[d[0]]]
            else:
                bad.append(ch)
        if bad:
            raise Untypeable("".join(dict.fromkeys(bad)))
        return strokes

    def chord(self, s):
        *mods, key = [p.strip().lower() for p in s.split("+")]
        if {"meta", "super"} & {*mods, key}:
            raise ChordDenied(s)  # Meta es del escritorio, no del agente
        if not key or any(m not in MODS for m in mods):
            raise ValueError(f"atajo mal formado: {s!r}")
        mods = ["ctrl" if m == "control" else m for m in mods]
        if "ctrl" in mods and "alt" in mods and (key in ("delete", "backspace") or key in [f"f{n}" for n in range(1, 13)]):
            raise ChordDenied(s)  # cambio de VT, reinicio de sesión
        if key in NAMED:
            code = NAMED[key]
        elif len(key) == 1 and (hit := self.table().get(key)) and not hit[0]:
            code = hit[1]
        else:
            raise ValueError(f"tecla desconocida en {s!r}")
        return tuple(dict.fromkeys(MODS[m] for m in mods)), code
