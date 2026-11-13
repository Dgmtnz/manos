"""Autocomprobación de spaces.py:  uv run --project . tests/test_spaces.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spaces
from spaces import MAX_EDGE, OffScreen, Space, StaleCapture, fit, new_capture, output_at, tokens, window_rect

OUTPUTS = [{"name": "eDP-1", "x": 0, "y": 616, "width": 1920, "height": 1080},
           {"name": "HDMI-A-1", "x": 1920, "y": 0, "width": 1080, "height": 1920}]  # girada a vertical


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    return False


def near(a, b, eps=1e-6):
    return all(abs(x - y) < eps for x, y in zip(a, b))


# the workspace capture: 3000x1920 arrives at 1568x1004, a 1.914 factor that must be inverted
space, w, h = new_capture("logical", (0, 0), 3000, 1920)
assert (w, h) == (1568, 1004), (w, h)
assert abs(space.factor - 1.914) < 0.001, space.factor
assert tokens(w, h) == 56 * 36 == 2016, tokens(w, h)
assert max(w, h) <= MAX_EDGE
assert near(space.to_space(784, 502), (1500.0, 960.46), 0.01), space.to_space(784, 502)
assert near(space.to_image(*space.to_space(784, 502)), (784, 502))  # round trip
assert fit(800, 600) == (800, 600, 1.0)  # a small capture is not upscaled

# the non-rectangular space: the bounding box has dead zones that belong to no output
assert output_at(OUTPUTS, 0, 0) is None  # (0,0) is on no output
assert output_at(OUTPUTS, 100, 100) is None  # above eDP-1, left of HDMI-A-1
assert output_at(OUTPUTS, 500, 700)["name"] == "eDP-1"
assert output_at(OUTPUTS, 2500, 1500)["name"] == "HDMI-A-1"  # below eDP-1's bottom, still on the portrait output
assert raises(OffScreen, output_at, [dict(OUTPUTS[0], scale=1.5)], 500, 700)  # scale != 1: formula not re-measured
assert raises(OffScreen, space.click, 0, 0, OUTPUTS)  # a click into a dead zone is rejected, not clamped

# the rotated output, captured on its own: 1080x1920 -> 882x1568
portrait, pw, ph = new_capture("logical", (1920, 0), 1080, 1920)
assert (pw, ph) == (882, 1568), (pw, ph)
assert near(portrait.click(100, 200, OUTPUTS), (2042.44897959, 244.89795918), 1e-6), portrait.click(100, 200)
assert near(portrait.to_image(*portrait.to_space(100, 200)), (100, 200))
assert output_at(OUTPUTS, *portrait.to_space(881, 1567))["name"] == "HDMI-A-1"  # the far corner is still on it

# CaptureWindow returns the CLIENT rect: 36 px of decoration on this theme
info = {"frameGeometry": {"x": 757.5, "y": 100.0, "width": 1920.0, "height": 1080.0},
        "clientGeometry": {"x": 757.5, "y": 136.0, "width": 1920.0, "height": 1044.0}}
x, y, cw, ch = window_rect(info)
assert (cw, ch) == (1920.0, 1044.0) and info["frameGeometry"]["height"] - ch == 36.0
win, iw, ih = new_capture("logical", (x, y), int(cw), int(ch))
assert y - info["frameGeometry"]["y"] == 36.0  # using frameGeometry would put every click on the titlebar
assert win.to_space(0, 0)[1] == 136.0

# KWin geometry is a RectF: float, and an int schema truncates it in silence
assert win.origin[0] == 757.5 and int(win.origin[0]) != win.origin[0]
assert win.to_space(0, 0)[0] == 757.5

# a click whose capture_id is not the latest of its space is rejected
stale = space
fresh, _, _ = new_capture("logical", (0, 0), 3000, 1920)
assert fresh.capture_id > stale.capture_id
assert raises(StaleCapture, stale.click, 10, 10)
assert near(fresh.click(10, 10), fresh.to_space(10, 10))
viewport, _, _ = new_capture("viewport", (0, 0), 1280, 720)  # another space does not invalidate this one
assert near(fresh.click(10, 10), fresh.to_space(10, 10)) and viewport.click(0, 0) == (0.0, 0.0)
assert viewport.factor == 1.0 and raises(StaleCapture, Space("viewport", (0, 0), 1.0, 0).click, 0, 0)

print(f"test_spaces OK  ({spaces.__file__})")
