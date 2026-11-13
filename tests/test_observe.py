"""Autocomprobación de browser/observe.py:  uv run --project . tests/test_observe.py

Mide la cosecha contra una verdad de terreno escrita aquí —el conjunto exacto de nombres que el
fixture tiene que dar, ni uno más ni uno menos— y comprueba las cuatro cosas que P0-C dejó decididas y
una que dejó sin medir:

- precisión y recall 1,000 contra el formulario de laboratorio, que es donde el join daba 0,553;
- `[tapado:…]` sale de un hit-test real sobre un elemento deliberadamente cubierto;
- la cosecha cruza un shadow root abierto;
- la cosecha cruza un iframe **de otro origen**, con su propia sesión CDP — el hueco que P0-C no midió;
- ni el XPath ni el `href` llegan nunca al listado.

El iframe cruzado se monta con dos nombres de host distintos (`localhost` y `127.0.0.1`) sobre un
`http.server` de la biblioteca estándar: ni red, ni dependencias, ni tocar ninguna página de nadie.
"""

import asyncio
import functools
import http.server
import json
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import refs
from browser import observe, sanitize
from browser.session import BrowserSession

FIXTURES = Path(__file__).parent / "fixtures"

# La verdad de terreno: los nombres de las dianas visibles y dentro del viewport, exactos. La igualdad
# de conjuntos asierta precisión y recall de una vez —un falso positivo mete un nombre de más y una
# diana perdida lo quita— y deja el fixture sin atributos que solo existan para puntuarlo.
DIANAS = {"Botón tapado", "Nombre completo", "Teléfono", "Provincia", "Acepto las condiciones",
          "Enviar", "Un enlace normal", "Catálogo 2026", "Botón ARIA", "Ayuda", "IBAN"}
DIANAS_IFRAME = {"Cupón", "Aplicar cupón"}


def serve() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def pct(xs, q):
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, round(q * (len(xs) - 1))))]


async def rows_of(session, viewport_only=True):
    """Las filas crudas por frame, sin pasar por refs ni por el saneado: para medir la verdad."""
    out = []
    sessions = await observe._frame_sessions(session)
    try:
        for cdp, tree, _own in sessions:
            for frame in observe._flatten(tree):
                t0 = time.perf_counter()
                harvest = await observe._eval(
                    cdp, frame["id"],
                    f"{observe.HARVEST}({json.dumps({'viewport_only': viewport_only})})")
                out.append((frame, harvest, (time.perf_counter() - t0) * 1000))
    finally:
        for cdp, _tree, own in sessions:
            if own:
                await cdp.detach()
    return out


async def main():
    port = serve()
    main_url = f"http://localhost:{port}/ground.html"
    inner_url = f"http://127.0.0.1:{port}/inner.html"

    session = await BrowserSession.start()
    try:
        cdp = await session.cdp()
        # Viewport fijo y alto: la verdad de terreno tiene que ser la misma corra donde corra, y el
        # tamaño real de la ventana lo decide KWin.
        await cdp.send("Emulation.setDeviceMetricsOverride",
                       {"width": 1280, "height": 2600, "deviceScaleFactor": 1, "mobile": False})
        page = session.page
        await page.goto(main_url)
        await page.evaluate(f"document.getElementById('marco').src = {json.dumps(inner_url)}")
        for _ in range(100):
            marco = [f for f in page.frames if f.url.endswith("inner.html")]
            if marco:
                await marco[0].wait_for_load_state()
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("el iframe de otro origen no cargó")

        # --- verdad de terreno ----------------------------------------------------------------------
        harvested = await rows_of(session)
        assert len(harvested) == 2, f"se esperaban 2 frames, salieron {len(harvested)}"
        oopif = len(harvested) == 2 and harvested[1][0]["id"] != harvested[0][0]["id"]
        main_frame, main_h, main_ms = harvested[0]
        sub_frame, sub_h, sub_ms = harvested[1]

        rows = main_h["els"]
        nombres = {r.get("n", "") for r in rows}
        print(f"principal: {len(rows)} filas de {len(DIANAS)} dianas")
        for r in rows:
            print(f"    {r['r']:18s} {r.get('n', '')!r:34s} len={r.get('vl', '-')} "
                  f"cov={r.get('cov', '-')} shadow={r.get('shadow', '-')}")
        assert nombres == DIANAS, f"de más: {nombres - DIANAS} · de menos: {DIANAS - nombres}"
        assert len(rows) == len(DIANAS), f"{len(rows)} filas para {len(DIANAS)} dianas: hay repetidas"

        # --- oclusión: un hit-test real, no una heurística -------------------------------------------
        covered = [r for r in rows if "cov" in r]
        assert len(covered) == 1, f"se esperaba 1 elemento tapado, hay {len(covered)}: {covered}"
        assert covered[0]["n"] == "Botón tapado"
        assert covered[0]["cov"] == "div#cookie-consent", covered[0]["cov"]
        print(f"oclusión: '{covered[0]['n']}' tapado por {covered[0]['cov']}  OK")

        # --- shadow root ------------------------------------------------------------------------------
        iban = [r for r in rows if r.get("n") == "IBAN"]
        assert iban and iban[0].get("shadow") == 1, "el campo del shadow root no salió marcado"
        assert "//" in iban[0]["xp"], f"el xpath no cruza la frontera: {iban[0]['xp']}"
        print(f"shadow: IBAN encontrado, xpath {iban[0]['xp']}  OK")

        # --- contrato de formulario que el AX no tiene -------------------------------------------------
        tel = [r for r in rows if r.get("nm") == "tel"][0]
        assert tel["pattern"] == "[0-9]{9}" and tel["desc"] == "Debe tener 9 dígitos"
        assert tel["valid"] == 0 and "patternMismatch" in tel["why"] and tel["msg"]
        prov = [r for r in rows if r.get("nm") == "prov"][0]
        assert prov["opts"] == [["MU", "Murcia"], ["MA", "Madrid"]], prov["opts"]
        print(f"formulario: pattern, validez real ({tel['why']}) y valores de <option>  OK")

        # --- iframe de otro origen ---------------------------------------------------------------------
        sub_rows = sub_h["els"]
        assert {r.get("n", "") for r in sub_rows} == DIANAS_IFRAME, sub_rows
        print(f"iframe cruzado: {len(sub_rows)} dianas en {sub_frame.get('url')}, "
              f"sesión propia={oopif}  OK")

        # --- el listado que ve el modelo ---------------------------------------------------------------
        sanitize.forget()
        observe.reset()
        result = await observe.observe(session)
        listing = "\n".join(result["listing"])
        print("\nlistado tal cual lo ve el modelo:")
        print("\n".join("    " + ln for ln in result["listing"]))
        assert result["elements"] == len(DIANAS) + len(DIANAS_IFRAME)
        assert "example.org" not in listing, "el href llegó al modelo"
        assert "/html[1]" not in listing, "el xpath llegó al modelo"
        assert "[tapado:div#cookie-consent]" in listing
        assert f"(iframe: 127.0.0.1:{port})" in listing
        assert "(shadow)" in listing
        assert result["skipped_hidden"] >= 2, result["skipped_hidden"]
        assert result["skipped_offviewport"] >= 1, result["skipped_offviewport"]

        # El ref lleva el xpath y el href, y se quedan en el servidor.
        ref_id = int(listing.split("]")[0].lstrip("["))
        assert refs.lookup(ref_id).xpath.startswith("/")
        enlace = [ln for ln in result["listing"] if "Un enlace normal" in ln][0]
        assert refs.lookup(int(enlace.split("]")[0].lstrip("["))).href == "https://example.org/destino"

        # find(): grep en vez de volcado
        filtrado = await observe.observe(session, find="IBAN")
        assert filtrado["shown"] == 1 and filtrado["elements"] == len(DIANAS) + len(DIANAS_IFRAME)
        # viewport_only=False saca el campo de abajo del todo, marcado
        todo = await observe.observe(session, viewport_only=False)
        assert todo["elements"] > result["elements"]
        assert "Campo muy abajo" in "\n".join(todo["listing"])
        print("listado: sin href, sin xpath, con find y con viewport_only  OK")

        # --- presupuesto ------------------------------------------------------------------------------
        ms, nbytes = [], []
        for _ in range(10):
            t0 = time.perf_counter()
            got = await observe.observe(session)
            ms.append((time.perf_counter() - t0) * 1000)
            nbytes.append(len("\n".join(got["listing"]).encode()))
        print(f"\nobserve() completa (2 frames, {result['elements']} elementos): "
              f"p50 {pct(ms, .5):.1f} ms · p95 {pct(ms, .95):.1f} ms · "
              f"{statistics.median(nbytes):.0f} B ≈ {statistics.median(nbytes) / 3.6:.0f} tokens")
        print(f"cosecha sola: frame principal {main_ms:.1f} ms · frame cruzado {sub_ms:.1f} ms")
    finally:
        await session.close()
        observe.reset()


asyncio.run(main())
print("\ntest_observe: OK")
