"""browser_extract por el protocolo, en el navegador de verdad:  uv run --project . tests/test_extract.py

Contra bench/verify/serve.py en un puerto libre (ventas.html y su API, socios.html), y `localhost` del mismo
servidor como «otro origen». Ficheros y auditoría a un directorio temporal. ~30 s (dos esperas de Retry-After).
"""

import asyncio
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "bench/verify"))

DATA = tempfile.mkdtemp(prefix="manos-extract-")
os.environ["XDG_DATA_HOME"] = DATA
os.environ["BENCH_HOME"] = DATA
os.chdir(DATA)  # el directorio de trabajo del servidor: ahí caen los `filename` relativos

from mcp import Client

import serve
import server
from browser import extract, sanitize

OUT = Path(DATA)
ROW = re.compile(r'\[(\d+)\] (\S+) ("(?:[^"\\]|\\.)*")')
VENTAS = [json.dumps(v, ensure_ascii=False) for v in serve.ventas()]
HITS = {"cero": 0}


class Handler(serve.Handler):
    """El de los fixtures, más un 429 con `Retry-After: 0` perpetuo: cada reintento tiene que gastar presupuesto."""

    def do_GET(self):
        if not self.path.startswith("/api/cero"):
            return super().do_GET()
        HITS["cero"] += 1
        self.send_response(429)
        self.send_header("Retry-After", "0")
        self.send_header("Content-Length", "0")
        self.end_headers()


SOCIOS = json.loads(re.search(r'id="datos">(.*?)</script>', (HERE.parent / "bench/fixtures/socios.html").read_text(),
                              re.S).group(1))


async def main(base: str, port: int):
    async with Client(server.mcp) as c:
        async def call(tool, **args):
            return (await c.call_tool(tool, args)).structured_content

        async def ex(**args):
            return await call("browser_extract", **args)

        try:
            assert (await call("browser_open", url=f"{base}/ventas.html"))["ok"]
            api = dict(source="fetch", url="/api/ventas?pagina={page}", path="data.items")

            # entera: 120 ventas en 5 páginas, idénticas byte a byte a las de la API, y solo 3 de muestra
            r = await ex(filename=str(OUT / "v.ndjson"), **api)
            assert r["ok"] and (r["count"], r["pages"], r["resumed_from"]) == (120, 5, None), r
            assert (OUT / "v.ndjson").read_text(encoding="utf-8") == "".join(l + "\n" for l in VENTAS)
            assert len(r["sample"]) == 3 and r["sample"][0] == serve.ventas()[0], r["sample"]
            assert "Hecho" in r["next"], r

            # reanudar: tope de 2, luego sigue en la 3 sin repetir; otra vez no añade nada
            r = await ex(filename="v2.ndjson", max_pages=2, **api)  # relativo: al directorio de trabajo
            assert r["ok"] and (r["count"], r["pages"]) == (50, 2) and "mismos argumentos" in r["next"], r
            r = await ex(filename="v2.ndjson", **api)
            assert r["ok"] and (r["count"], r["pages"], r["resumed_from"]) == (120, 5, 3), r
            r = await ex(filename="v2.ndjson", **api)
            assert r["ok"] and (r["count"], r["resumed_from"], r["sample"]) == (120, 6, []), r
            assert (OUT / "v2.ndjson").read_text(encoding="utf-8") == (OUT / "v.ndjson").read_text(encoding="utf-8")
            # una caída entre escribir y apuntar: lo de más se corta, no se duplica
            with open(OUT / "v2.ndjson", "a", encoding="utf-8") as f:
                f.write(VENTAS[0] + "\n")
            r = await ex(filename="v2.ndjson", **api)
            assert r["ok"] and r["count"] == 120, r
            assert (OUT / "v2.ndjson").read_text(encoding="utf-8") == (OUT / "v.ndjson").read_text(encoding="utf-8")

            # otros argumentos contra un estado que existe, y un fichero ajeno: error, nunca sobrescribir
            r = await ex(filename="v2.ndjson", source="fetch", url="/api/ventas?pagina={page}", path="data")
            assert r["error"]["type"] == "StateMismatch", r
            (OUT / "ajeno.ndjson").write_text("mío\n")
            r = await ex(filename="ajeno.ndjson", **api)
            assert r["error"]["type"] == "StateMismatch" and (OUT / "ajeno.ndjson").read_text() == "mío\n", r

            # parámetro de página que el sitio ignora (`page=` en vez de `pagina=`): para en la 2, no la repite mil veces
            r = await ex(filename="rep.ndjson", source="fetch", url="/api/ventas?page={page}", path="data.items")
            assert r["error"]["type"] == "PageRepeats" and "pagina=" in r["error"]["next"], r
            assert not (OUT / "rep.ndjson").exists() and not (OUT / "rep.ndjson.state.json").exists(), r
            r = await ex(filename="rep.ndjson", **api)  # mismo nombre, parámetro bueno
            assert r["ok"] and r["count"] == 120, r

            # mismo origen y nada más: otro host, file:, data:; y rutas vedadas
            for bad in (f"http://localhost:{port}/api/ventas?pagina=1", "file:///etc/passwd", "data:application/json,[1]",
                        "//localhost/api/ventas"):
                r = await ex(filename="x.ndjson", source="fetch", url=bad)
                assert r["error"]["type"] == "ValueError" and "mismo origen" in r["error"]["message"], (bad, r)
            assert not (OUT / "x.ndjson").exists()
            r = await ex(filename="~/.ssh/robado.ndjson", **api)
            assert r["error"]["type"] == "PathRefused", r
            r = await ex(filename="v3.ndjson", source="fetch", url="/api/ventas?pagina=1", path="data.nada")
            assert r["error"]["type"] == "ValueError" and "items" in r["error"]["message"], r

            # 429 con Retry-After: se respeta (cero reintentos antes de tiempo) y sale entera
            r = await ex(filename="c.ndjson", source="fetch", url="/api/ventas-cupo?pagina={page}", path="data.items")
            cupo = json.loads((OUT / "ventas-cupo.json").read_text())
            assert r["ok"] and r["count"] == 120, r
            assert cupo == {"429": 2, "early": 0}, cupo
            assert (OUT / "c.ndjson").read_text(encoding="utf-8") == (OUT / "v.ndjson").read_text(encoding="utf-8")

            # sin presupuesto de espera: RateLimited con el `next` de cómo seguir, y seguir no repite
            serve._cupo["hasta"].clear()
            extract.WAIT_MAX = 0.5
            r = await ex(filename="c2.ndjson", source="fetch", url="/api/ventas-cupo?pagina={page}", path="data.items")
            assert r["error"]["type"] == "RateLimited" and "página 2" in r["error"]["next"], r
            extract.WAIT_MAX = 30.0
            r = await ex(filename="c2.ndjson", source="fetch", url="/api/ventas-cupo?pagina={page}", path="data.items")
            assert r["ok"] and r["count"] == 120 and r["resumed_from"] == 2, r
            assert (OUT / "c2.ndjson").read_text(encoding="utf-8") == (OUT / "v.ndjson").read_text(encoding="utf-8")

            # Retry-After: 0 para siempre: la espera exponencial gasta el presupuesto y acaba en RateLimited
            extract.WAIT_MAX = 3.0
            r = await ex(filename="cero.ndjson", source="fetch", url="/api/cero?p={page}")
            assert r["error"]["type"] == "RateLimited" and HITS["cero"] == 3, (r, HITS)  # esperas de 1 y 2 s
            extract.WAIT_MAX = 30.0

            # una caída tras escribir la primera página y antes de apuntarla: el estado ya existía (0 bytes), así
            # que reanudar corta lo escrito y sigue sin repetir; sin él, el fichero quedaba huérfano para siempre
            write = extract._Sink.write

            def crash(self, recs):
                write(self, recs)
                raise RuntimeError("caída simulada")
            extract._Sink.write = crash
            try:
                r = await ex(filename="caida.ndjson", **api)
            finally:
                extract._Sink.write = write
            assert r["error"]["message"] == "caída simulada" and (OUT / "caida.ndjson.state.json").exists(), r
            assert (OUT / "caida.ndjson").read_text(encoding="utf-8").count("\n") == 25
            r = await ex(filename="caida.ndjson", **api)
            assert r["ok"] and (r["count"], r["pages"], r["resumed_from"]) == (120, 5, 1), r
            assert (OUT / "caida.ndjson").read_text(encoding="utf-8") == (OUT / "v.ndjson").read_text(encoding="utf-8")

            # la muestra pasa por scrub
            sanitize.remember(["Íñigo Muñoz"])
            r = await ex(filename="v4.ndjson", source="fetch", url="/api/ventas?pagina=1", path="data.items")
            assert r["ok"] and r["pages"] == 1 and "Íñigo" not in json.dumps(r, ensure_ascii=False), r
            assert "Íñigo Muñoz" in (OUT / "v4.ndjson").read_text(encoding="utf-8")  # el fichero, entero
            sanitize.forget()

            # «siguiente» nunca es un botón de formulario ni de envío: se pulsaría hasta 1000 veces
            assert (await call("browser_goto", url=f"{base}/form24.html"))["ok"]
            for boton in ("button[name=enviar]", "#boton-cv"):
                r = await ex(filename="form.ndjson", source="table", selector="form", next_button=boton)
                assert r["error"]["type"] == "ValueError" and "formulario" in r["error"]["message"], (boton, r)
            assert not (OUT / "form.ndjson").exists()

            # tabla con «Siguiente» a CSV, por selector; cada página enseña antes una fila «Cargando…» (no se guarda)
            assert (await call("browser_goto", url=f"{base}/socios.html"))["ok"]
            r = await ex(filename="s.csv", source="table", selector="#socios", next_button="#siguiente")
            assert r["ok"] and (r["count"], r["pages"]) == (47, 5), r
            with open(OUT / "s.csv", newline="", encoding="utf-8") as f:
                got = list(csv.reader(f))
            assert got[0] == list(SOCIOS[0]) and got[1:] == [list(s.values()) for s in SOCIOS], got[:3]

            # y por ref de browser_observe, empezando en la página 1 otra vez; tope 2 y reanudar
            assert (await call("browser_goto", action="reload"))["ok"]
            ob = await call("browser_observe", find="Siguiente")
            ref = next(m[1] for m in map(ROW.match, ob["listing"]) if m and json.loads(m[3]) == "Siguiente")
            r = await ex(filename="s2.ndjson", source="table", selector="#socios", next_button=ref, max_pages=2)
            assert r["ok"] and (r["count"], r["pages"]) == (20, 2), r
            r = await ex(filename="s2.ndjson", source="table", selector="#socios", next_button=ref)
            assert r["ok"] and (r["count"], r["pages"], r["resumed_from"]) == (47, 5, 3), r
            lines = (OUT / "s2.ndjson").read_text(encoding="utf-8").splitlines()
            assert [json.loads(l) for l in lines] == SOCIOS, lines[:2]

            # elementos repetidos con `fields`, sin paginar
            r = await ex(filename="f.ndjson", source="table", selector="#filas tr", fields={"n": "td", "loc": "td:nth-child(3)"})
            assert r["ok"] and r["count"] == 7 and r["sample"][0] == {"n": SOCIOS[40]["Nº socio"],
                                                                      "loc": SOCIOS[40]["Localidad"]}, r
            r = await ex(filename="g.ndjson", source="table", selector="#no-existe")
            assert r["error"]["type"] == "ValueError" and not (OUT / "g.ndjson").exists(), r
        finally:
            await call("browser_close")

    audit = [json.loads(l) for l in (OUT / "manos/audit.jsonl").read_text().splitlines()]
    ext = [l for l in audit if l["tool"] == "browser_extract"]
    assert ext and all(l["rung"] == "web" and l["action"] in ("fetch", "table") for l in ext), ext
    assert "ventas" not in json.dumps(ext), "la auditoría lleva urls o nombres de fichero"


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    asyncio.run(main(f"http://127.0.0.1:{srv.server_address[1]}", srv.server_address[1]))
finally:
    srv.shutdown()
    shutil.rmtree(DATA, ignore_errors=True)
print("test_extract OK")
