"""browser_download por el protocolo, en el navegador de verdad:  uv run --project . tests/test_download.py

Contra bench/verify/serve.py en un puerto libre. Ficheros y auditoría a un directorio temporal. ~10 s.
"""

import asyncio
import base64
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

DATA = tempfile.mkdtemp(prefix="manos-download-")
os.environ["XDG_DATA_HOME"] = DATA
os.environ["BENCH_HOME"] = DATA
os.chdir(DATA)  # los `filename` relativos caen aquí

from mcp import Client

import serve
import server

OUT = Path(DATA)
FIXTURE = HERE.parent / "bench/fixtures/envio-datos.json"  # serve.py lo sirve tal cual (las .html llevan testigo)
ROW = re.compile(r'\[(\d+)\] (\S+) ("(?:[^"\\]|\\.)*")')
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==")


async def main(base: str):
    async with Client(server.mcp) as c:
        async def dl(url, filename, **kw):
            return (await c.call_tool("browser_download", {"url": url, "filename": filename, **kw})).structured_content

        r = await dl(f"{base}/envio-datos.json", "a.json")
        assert r["error"]["type"] == "NotOpen", r  # sin navegador
        try:
            assert (await c.call_tool("browser_open", {"url": f"{base}/ventas.html"})).structured_content["ok"]

            # http: los bytes exactos, relativo al directorio de trabajo
            r = await dl(f"{base}/envio-datos.json", "a.json")
            assert r["ok"] and r["bytes"] == FIXTURE.stat().st_size and r["content_type"] == "application/json", r
            assert (OUT / "a.json").read_bytes() == FIXTURE.read_bytes() and r["saved_to"] == str(OUT / "a.json")

            # data: en base64 (una miniatura de Google Imágenes) y en texto con %xx
            r = await dl("data:image/png;base64," + base64.b64encode(PNG).decode(), str(OUT / "gato1.png"))
            assert r["ok"] and r["content_type"] == "image/png" and (OUT / "gato1.png").read_bytes() == PNG, r
            r = await dl("data:,hola%20mundo", "t.txt")
            assert r["ok"] and (OUT / "t.txt").read_bytes() == b"hola mundo", r

            # nunca pisa: el fichero que existe queda igual
            r = await dl("data:,otra%20cosa", "t.txt")
            assert r["error"]["type"] == "FileExistsError" and "filename" in r["error"]["next"], r
            assert (OUT / "t.txt").read_bytes() == b"hola mundo"

            # carpeta vedada, esquemas que no son http(s)/data:, 404 y data: roto: error y ningún fichero
            r = await dl("data:,x", str(Path.home() / ".manos-no-existe/x.txt"))
            assert r["error"]["type"] == "PathRefused", r
            for url in (FIXTURE.as_uri(), "javascript:alert(1)", "blob:http://127.0.0.1/x", "chrome://version"):
                r = await dl(url, "no.bin")
                assert r["error"]["type"] == "ValueError" and "http" in r["error"]["message"], (url, r)
            r = await dl(f"{base}/no-existe.json", "no.bin")
            assert r["error"]["type"] == "HttpError" and "404" in r["error"]["message"], r
            r = await dl("data:image/png;base64,abc", "no.bin")
            assert r["error"]["type"] == "ValueError", r
            assert not (OUT / "no.bin").exists()

            # por ref: la imagen de una miniatura enlazada (no su enlace), el enlace suelto, y un botón no
            assert (await c.call_tool("browser_open", {"url": f"{base}/descargas.html"})).structured_content["ok"]
            ob = (await c.call_tool("browser_observe", {})).structured_content
            r = {(m[2], json.loads(m[3])): int(m[1]) for m in map(ROW.match, ob["listing"]) if m}
            gato, datos, nada = r[("link", "Gato")], r[("link", "Datos del envío")], r[("button", "Nada")]
            got = await dl("", "gato2.png", ref=gato)
            assert got["ok"] and (OUT / "gato2.png").read_bytes() == PNG and got["content_type"] == "image/png", got
            got = await dl("", "b.json", ref=datos)
            assert got["ok"] and (OUT / "b.json").read_bytes() == FIXTURE.read_bytes(), got
            got = await dl("", "no.bin", ref=nada)
            assert got["error"]["type"] == "ValueError" and "ni imagen ni enlace" in got["error"]["message"], got
            for kw in ({"url": "data:,x", "ref": datos}, {"url": ""}):  # uno de los dos, exactamente
                got = (await c.call_tool("browser_download", {"filename": "no.bin", **kw})).structured_content
                assert got["error"]["type"] == "ValueError" and "uno de los dos" in got["error"]["message"], (kw, got)
            assert (await c.call_tool("browser_goto", {"action": "reload"})).structured_content["ok"]
            got = await dl("", "no.bin", ref=datos)  # navegó: el ref murió
            assert got["error"]["type"] == "StaleHandle", got
            assert not (OUT / "no.bin").exists()
        finally:
            await c.call_tool("browser_close", {})

    audit = [json.loads(l) for l in (OUT / "manos/audit.jsonl").read_text().splitlines()]
    got = [l for l in audit if l["tool"] == "browser_download"]
    assert got and {l["action"] for l in got} <= {"http", "data", None}, got
    blob = json.dumps(got)
    assert not any(w in blob for w in ("envio", "gato", "iVBOR", "127.0.0.1")), "la auditoría lleva urls o ficheros"


srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    asyncio.run(main(f"http://127.0.0.1:{srv.server_address[1]}"))
finally:
    srv.shutdown()
    shutil.rmtree(DATA, ignore_errors=True)
print("test_download OK")
