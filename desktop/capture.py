"""Captura de escritorio: el espacio lógico entero de una vez, recortado al objetivo, en JPEG con su Space.

`grab(target)` con target `"screen"`, `"active"`, `"output:<nombre>"` o el uuid de una ventana. Camino
rápido: ScreenShot2 `CaptureWorkspace` (`kwin.capture_workspace`); si el D-Bus falla, spectacle. La
negativa por ventana sensible visible la decide el servidor (`refuse`), y se aplica a cada estado leído.
Autocomprobación (solo en KWin anidada):  uv run --project . tests/test_desktop_nest.py
"""

import io
import os
import select
import subprocess
import tempfile
import time

import dbus
from PIL import Image

import kwin
import spaces
from desktop.eis import ScreenLocked, locked

DEADLINE = 5.0  # s para leer el pipe entero tras la respuesta de KWin
SPECTACLE_TIMEOUT = 15.0


class ScaleNotMeasured(RuntimeError): pass
class TopologyChanged(RuntimeError): pass
class NotVisible(RuntimeError): pass
class CaptureFailed(RuntimeError): pass


# de kwin-mcp screenshot.py:105-115 (MIT): QImage::Format → modo raw de Pillow. Los *32 son enteros
# little-endian (B,G,R,A en memoria); los *8888 van por bytes. Medido en 6.8.2 anidada: llega el 16.
_RAW = {4: ("RGB", "BGRX"), 5: ("RGBA", "BGRA"), 6: ("RGBA", "BGRA"), 16: ("RGB", "RGBX")}


def _screenshot2() -> Image.Image:
    # de kwin-mcp screenshot.py:1284-1395 (MIT), reescrito: KWin contesta y luego escribe el pipe desde
    # otro hilo (medido: la llamada vuelve en 3 ms, el pipe llega después), así que basta llamar y leer.
    r, w = os.pipe()
    try:
        try:
            meta = kwin.capture_workspace(w)
        finally:
            os.close(w)
        chunks, deadline = [], time.monotonic() + DEADLINE
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([r], [], [], left)[0]:
                raise CaptureFailed(f"KWin no terminó de escribir la captura en {DEADLINE:.0f} s.")
            if not (b := os.read(r, 1 << 20)):
                break
            chunks.append(b)
    finally:
        os.close(r)
    mode, raw = _RAW.get(meta.get("format", 6), (None, None))
    if not mode or meta.get("type", "raw") != "raw":
        raise CaptureFailed(f"Formato de captura desconocido: {meta}")
    try:
        return Image.frombuffer(mode, (meta["width"], meta["height"]), b"".join(chunks), "raw", raw,
                                meta["stride"], 1)
    except (KeyError, ValueError) as e:
        raise CaptureFailed(f"Captura malformada: {e}") from e


def _spectacle() -> Image.Image:
    # de kwin-mcp screenshot.py:1760-1821 (MIT): `-b -n -f -o`, sin puntero; el entorno es el del proceso
    with tempfile.TemporaryDirectory(prefix="manos-cap-") as d:
        out = os.path.join(d, "c.png")
        try:
            r = subprocess.run(["spectacle", "-b", "-n", "-f", "-o", out], capture_output=True, text=True,
                               timeout=SPECTACLE_TIMEOUT, stdin=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise CaptureFailed(f"spectacle: {e}") from e
        if r.returncode != 0 or not os.path.exists(out) or not os.path.getsize(out):  # 6.x sale 0 sin escribir
            raise CaptureFailed(f"spectacle no escribió la captura (salida {r.returncode}): {r.stderr.strip()[-300:]}")
        with Image.open(out) as im:
            return im.convert("RGB")


def _frame(st) -> tuple[Image.Image, str]:
    try:
        img, backend = _screenshot2(), "screenshot2"
    except dbus.DBusException:
        img, backend = _spectacle(), "spectacle"
    # de kwin-mcp screenshot.py:465-487 (MIT), la regla del ancla como comprobación: a escala 1, el píxel 0
    # de CaptureWorkspace y de la imagen combinada de spectacle es el lógico (0,0); con una sola salida,
    # spectacle ancla en esa salida. Las dos cosas se cumplen solo si el espacio empieza en (0,0).
    o = st["outputs"]
    if (min(x["x"] for x in o), min(x["y"] for x in o)) != (0, 0) or list(img.size) != st["virtual_screen"]:
        raise CaptureFailed(f"La captura ({backend}) mide {img.size} y la pantalla virtual {st['virtual_screen']} "
                            f"con origen en ({min(x['x'] for x in o)}, {min(x['y'] for x in o)}).")
    return img, backend


def _topology(st):
    return (st["outputs"], st["virtual_screen"], st["active"],
            [(w["uuid"], w["client"], w["minimized"]) for w in st["windows"]])


def _hit(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _box(target: str, st) -> tuple[tuple[float, float, float, float], list[str]]:
    """Rectángulo lógico (x, y, ancho, alto) del objetivo y clases de lo que tiene encima."""
    if target == "screen":
        return (0, 0, *st["virtual_screen"]), []
    if target.startswith("output:"):
        o = [o for o in st["outputs"] if o["name"] == target[7:]]
        if not o:
            raise ValueError(f"No hay salida {target[7:]!r}; hay {[o['name'] for o in st['outputs']]}.")
        return (o[0]["x"], o[0]["y"], o[0]["width"], o[0]["height"]), []
    uuid = st["active"] if target == "active" else target
    if uuid is None:
        raise NotVisible("No hay ventana activa.")
    idx = [i for i, w in enumerate(st["windows"]) if w["uuid"] == uuid]
    if not idx:
        raise NotVisible(f"KWin no tiene ninguna ventana normal con uuid {uuid}.")
    win = st["windows"][idx[0]]
    rect = tuple(win["client"])  # el cliente, no el marco: getWindowInfo no da clientGeometry
    if win.get("exclude_from_capture"):  # sale negra: la del navegador del agente, por ejemplo
        raise NotVisible(f"La ventana {uuid} está excluida de las capturas.")
    if win["minimized"] or not any(_hit(rect, (o["x"], o["y"], o["width"], o["height"])) for o in st["outputs"]):
        raise NotVisible(f"La ventana {uuid} está minimizada o fuera de toda salida.")
    above = [w for w in st["windows"][idx[0] + 1:] if not w["minimized"] and _hit(rect, w["frame"])]
    return rect, list(dict.fromkeys(w["resource_class"] for w in above))


def grab(target: str = "active", max_edge: int = spaces.MAX_EDGE, refuse=lambda st: None) -> tuple[bytes, dict]:
    """JPEG q85 del objetivo y su marco: `{space, origin, factor, capture_id, width, height, backend,
    occluded_by}`. El capture_id se acuña después de toda negativa: acuñar retira el anterior. `refuse(st)`
    corre sobre cada estado previo a una captura, el reintento incluido."""
    if locked():
        raise ScreenLocked("La pantalla está bloqueada.")
    for _ in range(2):
        before = kwin.state()
        refuse(before)
        if any(o["scale"] != 1 for o in before["outputs"]):
            raise ScaleNotMeasured("Hay una salida con escala distinta de 1: la captura no se ha medido así.")
        (x, y, w, h), occluded = _box(target, before)
        img, backend = _frame(before)
        if _topology(kwin.state()) == _topology(before):
            break
    else:
        raise TopologyChanged("Las salidas o las ventanas cambiaron durante la captura, dos veces.")
    # Recorte en enteros dentro de la imagen; el origen del Space es el del recorte de verdad
    x0, y0 = max(0, round(x)), max(0, round(y))
    x1, y1 = min(img.width, round(x + w)), min(img.height, round(y + h))
    if x1 <= x0 or y1 <= y0:
        raise NotVisible(f"El objetivo {target} cae fuera de la captura.")
    crop = img.crop((x0, y0, x1, y1))
    space, w2, h2 = spaces.new_capture("logical", (x0, y0), crop.width, crop.height, max_edge)
    if (w2, h2) != crop.size:
        crop = crop.resize((w2, h2), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue(), {"space": space.space, "origin": list(space.origin), "factor": space.factor,
                            "capture_id": space.capture_id, "width": w2, "height": h2,
                            "backend": backend, "occluded_by": occluded}
