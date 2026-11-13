"""Sirve los fixtures de formularios y graba lo que acaba escrito en cada campo.

Por qué existe. Un verificador es un proceso aparte que corre **después** del agente, y la regla del
proyecto es que un formulario no se envía nunca: no hay POST que inspeccionar, el navegador lo tiene el
servidor MCP por un pipe sin puerto TCP, y una página `file://` no puede dejar nada en el disco. Así que
el fixture se instrumenta a sí mismo: este servidor le inyecta un testigo que, en cada `input` y cada
`change`, manda el estado completo del formulario a `POST /state/<fixture>`, y aquí se escribe en
`$BENCH_HOME/<fixture>.json`. El verificador solo lee ese fichero y asegura lo que aseguraría una
persona mirando la pantalla.

Las páginas que lo necesitan se apuntan a sí mismas por el mismo `POST /state/<nombre>` (lo que pintaron,
los bytes de un fichero subido). Se sirven también los `.json` de bench/fixtures, sin testigo, para las que
piden sus datos por XHR, y `localhost:8921` es el segundo origen de facturas.html: el mismo servidor con
otro nombre de host basta para que el iframe sea de otro sitio y vaya en su propio proceso.

`/api/ventas?pagina=N` es la API paginada de ventas.html (`ventas()`, 25 por página, lista vacía al acabar) y
`/api/ventas-cupo` la misma con cupo: la primera petición de las páginas de `CUPO` da 429 con `Retry-After`, y
cualquier otra antes de que venza también (y cuenta como `early`); las de la propia página (`vista=1`) no tienen cupo. Lo servido queda en `$BENCH_HOME/ventas-cupo.json`.

El testigo se inyecta aquí y no en cada `.html` para que los seis fixtures sigan siendo formularios
limpios y legibles, y para que el testigo exista una sola vez.

    uv run --project . bench/verify/serve.py            # en primer plano, para mirarlo
    uv run --project . bench/verify/serve.py --start     # se queda de fondo y escribe su pid

`--start` enlaza el socket **antes** de irse al fondo, así que vuelve cuando el servidor ya responde
(sin `sleep` y sin carrera) y falla a la primera si el puerto está ocupado. El pid queda en
`$BENCH_HOME/serve.pid` y el `teardown` de cada tarea lo mata por ese pid: `pkill -f`/`pgrep -f` están
prohibidos en esta máquina porque emparejan la propia línea de órdenes que los invoca.

Autocomprobación:  uv run --project . bench/verify/serve.py --selftest
"""

import json
import math
import os
import re
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PORT = 8921
BENCH = Path(__file__).resolve().parents[1]
FIXTURES = BENCH / "fixtures"
HOME = Path(os.environ.get("BENCH_HOME") or BENCH / "run")
PIDFILE = HOME / "serve.pid"
SAFE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# .json para los datos que una página pide por XHR (envio.html): sin testigo, que no es una página
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json"}
POR_PAGINA, CUPO, RETRY = 25, (2, 4), 2  # la API de ventas: tamaño de página, páginas con 429 y su Retry-After (s)
_cupo: dict = {"429": 0, "early": 0, "hasta": {}}  # por proceso: cada tarea arranca su servidor


def ventas() -> list[dict]:
    """Las 120 ventas de /api/ventas, deterministas: la única copia (bench/verify/extraer.py las importa)."""
    clientes = ["Lucía Pérez", "Íñigo Muñoz", 'Bar "El Puerto", S.L.', "Zoë Martín", "Óscar Núñez", "Ana Gil"]
    return [{"id": f"V-{i:04d}", "cliente": clientes[i % 6], "importe": round(i * 37 % 1000 / 7, 2),
             "pagado": i % 3 != 0, "lineas": i % 4 + 1, "nota": None if i % 5 else "envío urgente"}
            for i in range(1, 121)]

# El testigo. `input` es `composed`, así que cruza un shadow root abierto y llega al documento; `change`
# no, por eso el barrido de `collect()` entra en cada `shadowRoot` en vez de fiarse del borboteo. Un
# radio sin marcar no emite fila, para que la clave del grupo lleve siempre la opción elegida.
BEACON = """
<script>
(() => {
  const ID = location.pathname.replace(/^\\/+|\\.html$/g, '');
  const SEL = 'input,select,textarea,[contenteditable=""],[contenteditable="true"]';
  let submitted = false, timer = 0;

  const collect = (root, out) => {
    for (const el of root.querySelectorAll(SEL)) {
      const key = el.name || el.id;
      if (!key) continue;
      const k = el.tagName === 'INPUT' ? el.type : el.tagName.toLowerCase();
      const o = {k: k};
      if (k === 'checkbox' || k === 'radio') {
        if (k === 'radio' && !el.checked) continue;
        o.checked = el.checked; o.value = el.value;
      } else if (k === 'file') {
        o.files = [...el.files].map(f => [f.name, f.size]);
      } else {
        o.value = el.isContentEditable ? el.innerText : el.value;
      }
      if (el.willValidate) {
        o.valid = el.checkValidity();
        if (!o.valid) o.msg = el.validationMessage;
      }
      if (k !== 'hidden' && el.offsetParent === null) o.hidden = true;
      out[key] = o;
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collect(el.shadowRoot, out);
    return out;
  };

  const send = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      fetch('/state/' + ID, {method: 'POST', body: JSON.stringify({
        submitted: submitted,
        form_valid: [...document.forms].every(f => f.checkValidity()),
        fields: collect(document, {})})});
    }, 200);
  };

  addEventListener('input', send, true);
  addEventListener('change', send, true);
  addEventListener('submit', e => { e.preventDefault(); submitted = true; send(); }, true);
  send();
})();
</script>
"""


class Handler(SimpleHTTPRequestHandler):
    def _api(self, name: str, query: str) -> None:
        try:
            pagina = int(parse_qs(query).get("pagina", ["1"])[0])
        except ValueError:
            self.send_error(400, "pagina no es un número")
            return
        if name == "ventas-cupo" and pagina in CUPO and "vista" not in parse_qs(query):  # la página no gasta cupo
            now, hasta = time.monotonic(), _cupo["hasta"].get(pagina)
            if hasta is None or now < hasta:
                _cupo["early"] += hasta is not None
                hasta = _cupo["hasta"].setdefault(pagina, now + RETRY)
                _cupo["429"] += 1
                HOME.mkdir(parents=True, exist_ok=True)
                (HOME / "ventas-cupo.json").write_text(json.dumps({"429": _cupo["429"], "early": _cupo["early"]}))
                self.send_response(429)
                self.send_header("Retry-After", str(math.ceil(hasta - now)))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        items = ventas()[(pagina - 1) * POR_PAGINA:pagina * POR_PAGINA] if pagina >= 1 else []
        body = json.dumps({"data": {"pagina": pagina, "items": items}}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path in ("/api/ventas", "/api/ventas-cupo"):
            self._api(parts.path.removeprefix("/api/"), parts.query)
            return
        name = self.path.lstrip("/").split("?")[0] or "index.html"
        page = (FIXTURES / name).resolve()
        if page.suffix not in TYPES or page.parent != FIXTURES or not page.is_file():
            self.send_error(404, "solo los fixtures de bench/fixtures")
            return
        body = page.read_bytes()
        if page.suffix == ".html":
            body = body.decode().replace("</body>", BEACON + "</body>").encode()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[page.suffix])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        name = self.path.removeprefix("/state/")
        if not SAFE.match(name):
            self.send_error(404, "ruta de estado no válida")
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        HOME.mkdir(parents=True, exist_ok=True)
        # Se reescribe entero y se valida al entrar: un cuerpo roto tiene que fallar aquí, donde se ve,
        # y no en el verificador, donde parecería que el agente no rellenó nada.
        (HOME / f"{name}.json").write_text(
            json.dumps(json.loads(raw), ensure_ascii=False, indent=1), encoding="utf-8")
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):   # a stderr, que el setup redirige a $BENCH_HOME/serve.log
        sys.stderr.write("serve: " + fmt % args + "\n")


def selftest() -> int:
    """Las dos propiedades que no son obvias: el testigo se inyecta y /state escribe lo que llega."""
    import threading
    import urllib.request

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(f"{base}/form24.html").read().decode()
        assert "/state/" in page and "addEventListener" in page, "el testigo no se inyectó"
        assert page.count("</body>") == 1, "el testigo duplicó el cierre de body"
        datos = urllib.request.urlopen(f"{base}/envio-datos.json")
        assert datos.headers["Content-Type"] == "application/json" and "codigo" in json.load(datos), \
            "los datos de XHR no se sirven tal cual"
        # el segundo origen de facturas.html: mismo servidor, `localhost` en vez de 127.0.0.1
        visor = urllib.request.urlopen(f"http://localhost:{srv.server_address[1]}/facturas-visor.html").read()
        assert b"/state/" in visor, "localhost no llega al servidor: el iframe entre orígenes no cargaría"

        # la API de ventas: 25 por página, la 5ª con 20, la 6ª vacía; con cupo, 429 + Retry-After en la 2ª
        pag = lambda n, api="ventas": json.load(urllib.request.urlopen(f"{base}/api/{api}?pagina={n}"))["data"]["items"]
        assert [len(pag(n)) for n in range(1, 7)] == [25, 25, 25, 25, 20, 0]
        assert sum((pag(n) for n in range(1, 6)), []) == ventas()
        try:
            pag(2, "ventas-cupo")
            raise AssertionError("la página 2 con cupo no dio 429")
        except urllib.error.HTTPError as exc:
            assert exc.code == 429 and exc.headers["Retry-After"] == str(RETRY), (exc.code, exc.headers)
        assert len(pag(1, "ventas-cupo")) == 25  # la 1 no tiene cupo

        state = {"submitted": False, "form_valid": True, "fields": {"nombre": {"value": "Lucía"}}}
        req = urllib.request.Request(f"{base}/state/selftest", data=json.dumps(state).encode(),
                                     method="POST")
        assert urllib.request.urlopen(req).status == 204
        written = json.loads((HOME / "selftest.json").read_text())
        assert written == state, written

        for bad in ("/state/../../etc/passwd", "/etc/passwd", "/../profile.example.toml", "/justificante-pago.pdf"):
            try:
                urllib.request.urlopen(f"{base}{bad}")
                raise AssertionError(f"{bad} no se rechazó")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, (bad, exc.code)
    finally:
        srv.shutdown()
        (HOME / "selftest.json").unlink(missing_ok=True)
        (HOME / "ventas-cupo.json").unlink(missing_ok=True)
    print("serve: OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        # El fallo que de verdad pasa: un servidor de una tarea anterior que el teardown no mató.
        raise SystemExit(f"serve: el puerto {PORT} está ocupado ({exc}). "
                         f'Para el anterior con: kill "$(cat {PIDFILE})"')
    if "--start" in sys.argv:
        pid = os.fork()                    # el socket ya está enlazado: el padre vuelve cuando responde
        if pid:
            HOME.mkdir(parents=True, exist_ok=True)
            PIDFILE.write_text(str(pid))
            print(f"serve: pid {pid} en http://127.0.0.1:{PORT}/ · estado en {HOME}")
            raise SystemExit(0)
        os.setsid()
    srv.serve_forever()
