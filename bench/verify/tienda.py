"""¿Dejó en el carrito 3 unidades del taladro pedido, y nada más, sin hacer el pedido?  (bench/tasks/web-webmcp-35.json)

La tienda (bench/fixtures/tienda.html) es tediosa a mano: 8 páginas de 10, sin buscador, y «Añadir» suma una unidad.
Pero declara tools WebMCP (`buscar_producto`, `anadir_al_carrito`, `ver_carrito`, `confirmar_pedido`), que hacen lo
mismo en una llamada. Lo que miraría una persona: el carrito con «Taladro percutor 800 W» × 3, sin el reacondicionado
de la página 3 y sin el pedido confirmado. Pasa por los dos caminos; la auditoría solo dice cuál se tomó.

El carrito lo apunta la página en `$BENCH_HOME/tienda-carrito.json`; los datos salen del propio fixture.
"""

import json
import re
from pathlib import Path

import comun

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tienda.html"
bloque = re.search(r'<script type="application/json" id="datos">(.*?)</script>', FIXTURE.read_text(encoding="utf-8"),
                   re.S)
datos = json.loads(bloque.group(1))
sku = next(p["sku"] for p in datos if p["nombre"] == "Taladro percutor 800 W")

ruta = comun.HOME / "tienda-carrito.json"
assert ruta.exists(), "la tienda no apuntó su carrito: ¿se abrió tienda.html?"
estado = json.loads(ruta.read_text(encoding="utf-8"))
assert not estado["pedido"], "se confirmó el pedido, y solo se pidió dejarlo en el carrito"
nombres = {p["sku"]: p["nombre"] for p in datos}
puesto = {nombres.get(k, k): v for k, v in estado["lineas"].items()}
assert estado["lineas"] == {sku: 3}, f"el carrito tiene {puesto} en vez de 3 × Taladro percutor 800 W"

audit = comun.auditoria()
usos = [l for l in audit or [] if l["tool"] == "browser_page_tool" and l["ok"]]
via = "sin auditoría" if audit is None else (f"browser_page_tool ({len(usos)} llamadas)" if usos else "a mano (clics)")
print(f"web-webmcp-35: OK · 3 × {sku} en el carrito, sin pedido · vía {via}")
