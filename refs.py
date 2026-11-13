"""El modelo de handles de manos: uno solo, no tres.

Un ref es `{frame_id, xpath, role, name}` (más la url del frame) guardado en el servidor; el modelo solo ve un
entero corto y opaco. El `xpath` (absoluto y cualificado por iframe y shadow root) es la parte duradera y reproducible.
**`xpath` y `href` nunca se serializan al modelo**: emitirlos duplica el payload para nada que el modelo
pueda usar, y es lo que ponía la cosecha por encima del join en tokens.

El ámbito es el frame: volver a observarlo retira sus ids, y `forget()` los retira al navegar
(`Page.frameNavigated`) o al limpiarse los contextos (`Runtime.executionContextsCleared`), que es lo que
hace `browser.observe.reset()`.

Antes de actuar, `browser/act.py` no relee rol y nombre: comprueba la identidad del nodo (el mismo mundo
aislado, el mismo elemento de `__MANOS_NODES`, conectado y con el mismo xpath), que es más estricto.

Autocomprobación:  uv run --project . tests/test_refs.py
"""

from dataclasses import dataclass
from itertools import count


class StaleHandle(LookupError):
    """El ref ya no sirve o ya no es lo que era; el mensaje dice qué hacer y es para el modelo."""


@dataclass(frozen=True)
class Ref:
    """Un elemento. `xpath` y `href` son estado del servidor y no salen de aquí."""

    frame_id: str
    xpath: str
    role: str
    name: str
    href: str = ""
    # Solo del navegador, para `browser/act.py`: el índice en `__MANOS_NODES`, el contexto del mundo aislado que lo
    # acuñó (otro mundo = la página navegó: el ref está muerto) y el nivel de secreto de la fila (`sanitize`).
    i: int = -1
    world: int = 0
    sec: str = ""
    url: str = ""  # la url del frame al observarlo: con el xpath, el rol y el nombre es el localizador de replay.py


_refs: dict[int, Ref] = {}        # ref id -> Ref
_scopes: dict[str, list[int]] = {}  # frame_id -> its ids
_ids = count(1)  # never reused, so a stale ref fails instead of acting on another element
# ponytail: the map is bounded by observe() — re-observing a frame replaces its ids, and reset() drops
# every frame on navigation and on close. An iframe that appears and vanishes without the page ever
# navigating leaks its ids until then, same as observe's own _worlds and _secrets. If that ever matters,
# the fix is for observe() to forget the frames it no longer sees, not an LRU over scopes.


def register(frame_id: str, refs: list[Ref], max_refs: int | None = None) -> tuple[list[int], int]:
    """Da ids nuevos a los refs y retira los de la observación anterior del mismo frame.

    Devuelve (ids, omitidos); `max_refs` recorta la cola y el contador dice cuántos se quedaron fuera.
    """
    forget(frame_id)
    kept = refs if max_refs is None else refs[:max_refs]
    ids = [next(_ids) for _ in kept]
    _refs.update(zip(ids, kept))
    _scopes[frame_id] = ids
    return ids, len(refs) - len(kept)


def forget(frame_id: str) -> None:
    """Invalida todo lo observado en un frame."""
    for i in _scopes.pop(frame_id, ()):
        del _refs[i]


def lookup(ref_id: int) -> Ref:
    if ref_id not in _refs:
        raise StaleHandle(f"[{ref_id}] ya no existe: un ref vale hasta la siguiente observación de su ámbito "
                          "(un frame o una ventana). Vuelve a observar.")
    return _refs[ref_id]
