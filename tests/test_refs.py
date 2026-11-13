"""Autocomprobación de refs.py:  uv run --project . tests/test_refs.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import refs
from refs import Ref, StaleHandle, forget, lookup, register


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return str(e)
    return ""


def el(n, role="button", name="Aceptar todo"):
    return Ref("F1", f'/html/body/div[{n}]', role, name, href="https://ejemplo.es/privacidad")


ids, omitted = register("F1", [el(1), el(2)])
assert omitted == 0 and len(ids) == 2 and ids[1] == ids[0] + 1
assert lookup(ids[1]).xpath == "/html/body/div[2]"

# observing the same frame again retires the previous ids, and no id is ever reused
again, _ = register("F1", [el(9)])
assert again[0] > ids[1] and lookup(again[0]).xpath == "/html/body/div[9]"
assert "Vuelve a observar" in raises(StaleHandle, lookup, ids[0])

# the budget cuts the tail and says how many were left out
few, omitted = register("F2", [el(i) for i in range(10)], max_refs=4)
assert len(few) == 4 and omitted == 6 and lookup(few[3]).xpath == "/html/body/div[3]"
assert raises(StaleHandle, lookup, few[3] + 1)

# xpath and href stay on the server: not even the error message the model reads carries them
_msg = raises(StaleHandle, lookup, ids[0])
assert _msg and "/html/body" not in _msg and "ejemplo.es" not in _msg, _msg

# Page.frameNavigated / Runtime.executionContextsCleared kill that frame and nothing else
forget("F1")
assert raises(StaleHandle, lookup, again[0])
assert lookup(few[0]).frame_id == "F1" and "F2" in refs._scopes  # F2 is untouched
assert len(refs._refs) == len(few), refs._refs  # forgetting a frame drops its refs too

print(f"test_refs OK  ({refs.__file__})")
