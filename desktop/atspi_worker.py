"""Trabajador AT-SPI de manos: el único proceso que carga libatspi.

`Atspi.init()` aborta el proceso entero con el bus de accesibilidad roto, así que libatspi vive aquí, en
`/usr/bin/python3 -I` (el Python que trae gi), sin importar nada de manos y hablando JSON por líneas con
`desktop/atspi.py`. `gi` se importa dentro de `main()`: el resto del módulo se importa sin él.

Peticiones:  {"op": "snapshot", win, pid, caption, size, find, max, coords, keep?}   (keep: solo filas, sin handles; find/coords opcionales)
             {"op": "act", h, gen, role, name, action, value}   (action: press|focus|set_text|state|<nombre>)
Respuestas:  el resultado, o {"error": <tipo>, "msg": <texto para el modelo>}.
"""

import json
import os
import sys
import warnings
from itertools import count

Atspi = None  # se carga en main()
GEN = 0
SHOWN = ("focused", "checked", "selected", "expanded", "pressed")  # los estados que salen en el listado
PRESS = ("press", "click", "activate", "toggle", "jump")  # lo que vale como `press`, por orden
MAX_TEXT = 200

_handles: dict[int, object] = {}  # handle -> Atspi.Accessible
_by_win: dict[str, list[int]] = {}  # uuid de ventana -> sus handles; volver a leerla los retira
_ids = count(1)


class Err(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


def _norm(name):
    """`SetFocus` -> `setfocus`: el nombre que sale en el listado y el que acepta `act`."""
    return "".join(c for c in name.lower() if c.isalnum())


# de kwin-mcp accessibility.py:1107-1123 (MIT)
def _caption_consistent(caption: str, name: str) -> bool:
    """Caption check applies only when both sides are non-empty.

    Both sides are compared with whitespace runs collapsed to one space, the
    same way KWin simplifies a client title into its caption: KWrite 26.08
    names its AT-SPI frame 'file.txt  — KWrite' (two spaces) while KWin shows
    'file.txt — KWrite'.

    Accepts exact equality or the KDE " — App" suffix KWin shows for KDE apps
    (AT-SPI 'Open File' vs KWin 'Open File — KWrite'). A plain prefix is never
    accepted: 'Document' must not match 'Document 2 — KWrite'.
    """
    n, c = " ".join(name.split()), " ".join(caption.split())
    if not n or not c:
        return True
    return c == n or c.startswith(n + " — ")


# de kwin-mcp accessibility.py:822-830 (MIT), sin el docstring
def _state_nicks(element) -> list[str]:
    return [state.value_nick for state in element.get_state_set().get_states() if state.value_nick]


# de kwin-mcp accessibility.py:1125-1134 (MIT)
def _window_rect(element):
    """Window-local extents of an AT-SPI element, or None when unavailable."""
    try:
        component = element.get_component_iface()
        if component is None:
            return None
        rect = component.get_extents(Atspi.CoordType.WINDOW)
        return rect.x, rect.y, rect.width, rect.height
    except Exception:
        return None


def _shown(states):
    return [s for s in SHOWN if s in states] + ([] if "enabled" in states else ["disabled"])


def _actions(acc):
    ai = acc.get_action_iface()
    if ai is None:
        return None, []
    return ai, [_norm(Atspi.Action.get_action_name(ai, i) or "") for i in range(Atspi.Action.get_n_actions(ai))]


def _toplevel(pid, caption, size):
    """La ventana de nivel superior de `pid` que casa con la de KWin; si no hay exactamente una, error."""
    desk = Atspi.get_desktop(0)
    n = desk.get_child_count()
    if n < 0:  # el fallo silencioso: con el registro roto da -1 y un range() no itera
        raise Err("A11yDown", "El registro AT-SPI no contesta (child_count = -1).")
    apps = []
    for i in range(n):
        try:
            a = desk.get_child_at_index(i)
            if a is not None and a.get_process_id() == pid:
                apps.append(a)
        except Exception:
            pass  # una app colgada o muerta no tapa a las demás
    if not apps:
        raise Err("NotInA11y", f"El proceso {pid} no está en el bus de accesibilidad.")
    # adaptado de kwin-mcp accessibility.py:1060-1089 (MIT): pid, título y tamaño del cliente, sin atajos
    tops = [a.get_child_at_index(i) for a in apps for i in range(max(a.get_child_count(), 0))]
    tops = [t for t in tops if t is not None and _caption_consistent(caption, t.get_name() or "")]
    tops = [t for t in tops if (r := _window_rect(t)) and abs(r[2] - size[0]) <= 1 and abs(r[3] - size[1]) <= 1]
    if not tops:
        raise Err("NoToplevel", f"El proceso {pid} está en el bus, pero ninguna ventana suya casa con la de KWin.")
    if len(tops) > 1:
        raise Err("AmbiguousWindow", f"{len(tops)} ventanas del proceso {pid} casan con la de KWin.")
    return tops[0]


def _row(acc, coords):
    """La fila de un nodo, o None si no tiene nada que leer ni que hacer."""
    # adaptado de kwin-mcp accessibility.py:1137-1221 (MIT): acciones y texto por la clase de la interfaz
    states = set(_state_nicks(acc))
    if "showing" not in states:
        return None
    role, name = acc.get_role_name() or "unknown", acc.get_name() or ""
    acts = [a for a in _actions(acc)[1] if a and a != "setfocus"]  # foco: `focus`, sin ruido en cada fila
    row = {"role": role, "name": name, "actions": acts, "states": _shown(states)}
    ti = acc.get_text_iface()
    if ti is not None:
        n = Atspi.Text.get_character_count(ti)
        if "editable" in states or role == "password text":  # el contenido de un campo nunca sale
            row["val"], row["secret"] = f"len={n}", True
        elif n:
            text = Atspi.Text.get_text(ti, 0, min(n, MAX_TEXT))
            if text.strip() and text != name:
                row["val"] = "=" + json.dumps(text + ("…" if n > MAX_TEXT else ""), ensure_ascii=False)
    if not (name or acts or "val" in row):
        return None
    if coords:
        row["rect"] = _window_rect(acc)
    return row


def snapshot(req):
    top = _toplevel(req["pid"], req["caption"], req["size"])
    rule = Atspi.MatchRule.new(Atspi.StateSet.new([]), Atspi.CollectionMatchType.ALL, {},
                               Atspi.CollectionMatchType.ALL, list(range(Atspi.Role.LAST_DEFINED)),
                               Atspi.CollectionMatchType.ANY, [], Atspi.CollectionMatchType.ALL, False)
    col = top.get_collection_iface()
    found = col.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 0, True) if col is not None else []
    if not found and top.get_child_count() > 0:
        raise Err("A11yEmpty", "La ventana tiene hijos pero la búsqueda AT-SPI no devolvió ninguno.")
    find, rows = req.get("find", "").lower(), []
    for acc in found:
        try:
            row = _row(acc, req.get("coords", False))
        except Exception:
            continue  # un nodo que muere a mitad de lectura
        if row and find in f'{row["role"]} {row["name"]} {row.get("val", "")}'.lower():
            rows.append((acc, row))
    if req.get("keep"):  # el detector de bucles lee el árbol sin retirar los handles del modelo
        return {"rows": [row for _, row in rows]}
    for h in _by_win.pop(req["win"], ()):
        _handles.pop(h, None)
    shown = rows[:req["max"]]
    for acc, row in shown:
        row["h"] = next(_ids)
        _handles[row["h"]] = acc
    _by_win[req["win"]] = [row["h"] for _, row in shown]
    return {"rows": [row for _, row in shown], "nodes": len(rows), "secrets": sum("secret" in r for _, r in rows),
            "gen": GEN}


def act(req):
    acc = _handles.get(req["h"]) if req["gen"] == GEN else None
    if acc is None:
        raise Err("StaleHandle", "ya no existe (la ventana se volvió a leer o el trabajador AT-SPI es otro).")
    try:
        states, role, name = set(_state_nicks(acc)), acc.get_role_name(), acc.get_name() or ""
    except Exception:
        raise Err("StaleHandle", "ya no existe (el elemento se destruyó).") from None
    if "defunct" in states:
        raise Err("StaleHandle", "ya no existe (el elemento se destruyó).")
    if (role, name) != (req["role"], req["name"]):
        raise Err("StaleHandle", f"cambió (era {req['role']} {req['name']!r}, ahora {role} {name!r}).")
    a, out = req["action"], {}
    if a == "focus":
        comp = acc.get_component_iface()
        if comp is None or not Atspi.Component.grab_focus(comp):
            raise Err("NoSuchAction", "no acepta el foco.")
    elif a == "set_text":
        et = acc.get_editable_text_iface()
        if et is None:
            raise Err("NoSuchAction", "no es un campo de texto editable.")
        Atspi.EditableText.set_text_contents(et, req["value"])
        ti = acc.get_text_iface()
        out["verified"] = ti is not None and Atspi.Text.get_text(ti, 0, -1) == req["value"]
    elif a != "state":
        ai, names = _actions(acc)
        want = PRESS if a == "press" else (_norm(a),)
        i = next((names.index(w) for w in want if w in names), None)
        if i is None:
            raise Err("NoSuchAction", f"no tiene la acción {a!r}; tiene: {', '.join(names) or 'ninguna'}.")
        out["invoked"] = bool(Atspi.Action.do_action(ai, i))
    out["after"] = {"role": role, "states": _shown(set(_state_nicks(acc)))}
    return out


def main():
    global Atspi, GEN
    GEN = int(sys.argv[sys.argv.index("--generation") + 1])
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import GLib
    from gi.repository import Atspi as _atspi
    Atspi = _atspi
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    Atspi.set_timeout(2000, 2000)
    Atspi.init()
    ctx = GLib.MainContext.default()
    # de kwin-mcp accessibility.py:1266-1291 (MIT), recortado: las respuestas por un dup de stdout, y
    # lo que imprima cualquier biblioteca, a stderr
    responses = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        while ctx.pending():  # eventos pendientes: la caché de libatspi al día
            ctx.iteration(False)
        try:
            req = json.loads(line)
            resp = (snapshot if req["op"] == "snapshot" else act)(req)
        except Err as e:
            resp = {"error": e.kind, "msg": str(e)}
        except Exception as e:
            resp = {"error": "WorkerError", "msg": f"{type(e).__name__}: {e}"}
        responses.write(json.dumps(resp, ensure_ascii=False) + "\n")
        responses.flush()


if __name__ == "__main__":
    main()
