"""Contrato de coordenadas de manos: un valor tipado que viaja en cada captura y se exige en cada clic.

Hay cinco espacios en juego. El lógico de la sesión de KWin, que en esta máquina es un 3000x1920 **no
rectangular** con zonas muertas (eDP-1 en 0,616 1920x1080 y HDMI-A-1 en 1920,0 1080x1920, girada a
vertical), y cuyo origen no está garantizado en (0,0): (0,0) no cae en ninguna salida. El `ei_region` de
cada salida. El documento de CDP. El viewport de CDP en píxeles CSS. Y la imagen ya reescalada que ve el
modelo: el lado largo no puede pasar de 1568, así que una captura del espacio entero le llega a 1568x1004
y hay que invertir un factor de 1,914 para volver a coordenadas lógicas.

Un `Space` dice: el píxel (0,0) de la imagen que ve el modelo es `origin` en el espacio `space`, y cada
píxel de la imagen vale `factor` unidades de ese espacio. `Space.click()` es el único camino de imagen a
espacio: valida que la captura citada siga siendo la última y que el punto caiga en una salida real.

Autocomprobación:  uv run --project . tests/test_spaces.py
"""

import math
from dataclasses import dataclass
from itertools import count
from typing import Literal

MAX_EDGE = 1568  # long edge the model accepts; a bigger image is REJECTED with a validation error, never auto-scaled
TOKEN_CELL = 28  # the image costs ceil(w/28)*ceil(h/28) tokens


class StaleCapture(RuntimeError):
    """La captura citada ya no es la última de su espacio; el mensaje es para el modelo."""


class OffScreen(ValueError):
    """El punto no cae en ninguna salida; el mensaje es para el modelo."""


_ids = count(1)  # never reused, so a stale capture_id fails instead of landing on the new geometry
_latest: dict[str, int] = {}  # space -> last capture_id minted for it
# ponytail: one slot per space name, so two pages captured at once would invalidate each other;
# key _latest by (space, target_id) the day a second page is live.


@dataclass(frozen=True)
class Space:
    """El marco de una captura. Va en el retorno de cada captura y se exige en cada clic."""

    space: Literal["logical", "image", "viewport"]
    origin: tuple[float, float]
    factor: float
    capture_id: int

    def to_space(self, x: float, y: float) -> tuple[float, float]:
        """De píxel de la imagen a coordenada del espacio."""
        return self.origin[0] + x * self.factor, self.origin[1] + y * self.factor

    def to_image(self, x: float, y: float) -> tuple[float, float]:
        """De coordenada del espacio a píxel de la imagen."""
        return (x - self.origin[0]) / self.factor, (y - self.origin[1]) / self.factor

    def click(self, x: float, y: float, outputs: list[dict] | None = None) -> tuple[float, float]:
        """Convierte un clic del modelo, o lo rechaza. Único camino permitido de imagen a espacio."""
        if self.capture_id != _latest.get(self.space):
            raise StaleCapture(f"La captura {self.capture_id} ya no es la última de '{self.space}': la pantalla "
                               "cambió desde entonces. Vuelve a capturar y usa el Space nuevo.")
        point = self.to_space(x, y)
        if outputs is not None and output_at(outputs, *point) is None:
            raise OffScreen(f"El punto ({point[0]:.1f}, {point[1]:.1f}) no cae en ninguna salida: el espacio "
                            "lógico no es rectangular y tiene zonas muertas. Elige un punto dentro de una ventana.")
        return point


def fit(width: int, height: int, max_edge: int = MAX_EDGE) -> tuple[int, int, float]:
    """Tamaño al que hay que reescalar una captura y el factor que lo invierte (unidades por píxel)."""
    factor = max(width, height) / max_edge
    if factor <= 1:
        return width, height, 1.0
    return max(1, math.floor(width / factor + 0.5)), max(1, math.floor(height / factor + 0.5)), factor


def tokens(width: int, height: int) -> int:
    """Lo que cuesta la imagen en tokens."""
    return math.ceil(width / TOKEN_CELL) * math.ceil(height / TOKEN_CELL)


def new_capture(space: str, origin: tuple[float, float], width: int, height: int,
                max_edge: int = MAX_EDGE) -> tuple[Space, int, int]:
    """Marca una captura como la última de su espacio y devuelve (Space, ancho, alto) ya reescalados."""
    w, h, factor = fit(width, height, max_edge)
    capture_id = next(_ids)
    _latest[space] = capture_id
    return Space(space, (float(origin[0]), float(origin[1])), factor, capture_id), w, h


def window_rect(info: dict) -> tuple[float, float, float, float]:
    """Rectángulo lógico de una captura de ventana, como (x, y, ancho, alto).

    `CaptureWindow` devuelve el rectángulo del CLIENTE, no el del marco: en este tema son 36 px de
    decoración, así que tomar el origen de `frameGeometry` desvía cada clic la altura de la barra de
    título. Y la geometría de KWin es un `RectF`: viene en float (se ha leído un x=757,5), de modo que
    tratarla como int la trunca en silencio.
    """
    g = info["clientGeometry"]  # KeyError ruidoso antes que un offset silencioso de 36 px
    return float(g["x"]), float(g["y"]), float(g["width"]), float(g["height"])


def output_at(outputs: list[dict], x: float, y: float) -> dict | None:
    """La salida que contiene el punto lógico, o None. Las dianas se enumeran por salida, nunca desde el
    rectángulo envolvente: el envolvente incluye zonas muertas que no pertenecen a ninguna pantalla."""
    for o in outputs:
        if o.get("scale", 1) != 1:  # every formula here was measured at scale 1 and none has been re-measured
            raise OffScreen(f"La salida {o.get('name')} tiene escala {o['scale']}: manos no devuelve "
                            "coordenadas hasta que la fórmula se vuelva a medir con escala fraccionaria.")
        if o["x"] <= x < o["x"] + o["width"] and o["y"] <= y < o["y"] + o["height"]:
            return o
    return None
