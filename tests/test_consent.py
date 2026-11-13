"""Los banners de cookies (browser/consent.js):  uv run --project . tests/test_consent.py

Cada página apunta en `window.clic` qué botón se pulsó. Las de CMP conocido y la genérica en español tienen que
acabar con el rechazo pulsado y nada aceptado, también la que llega tarde, la que se muestra cambiando el estilo,
la de shadow DOM y la de un iframe de otro origen (`localhost` contra `127.0.0.1`, su propio proceso). Un banner
sin opción de rechazo y un «Rechazar» que no es de cookies se quedan sin tocar, como el
«No acepto» de un paso de consentimiento, el «Decline» dentro de un `cmp-container` de AEM, un contenedor «cookie» con
un campo de texto y uno de RGPD cuyo texto no habla de cookies. browser_observe lo dice por frame.
"""

import asyncio
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser import observe
from browser.session import BrowserSession

HEAD = """<!doctype html><meta charset="utf-8"><title>{t}</title>
<script>window.clic = []; function r(b, v) {{ clic.push(v); (b.closest('[data-banner]') || b.getRootNode().host)?.remove(); }}</script>
<body><h1>{t}</h1><p>Contenido de la página.</p>"""


def boton(texto: str, valor: str, attrs: str = "") -> str:
    return f'<button {attrs} onclick="r(this, \'{valor}\')">{texto}</button>'


PAGES = {
    # llega 300 ms después de cargar, como el SDK de verdad
    "onetrust": """<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend', `
      <div id="onetrust-consent-sdk" data-banner><div id="onetrust-banner-sdk">Usamos cookies.
        %s %s %s</div></div>`), 300)</script>""" % (
        boton("Configurar", "config", 'id="onetrust-pc-btn-handler"'),
        boton("Rechazar todas", "reject", 'id="onetrust-reject-all-handler"'),
        boton("Aceptar todas las cookies", "accept", 'id="onetrust-accept-btn-handler"')),
    # ya está en el DOM, oculto; se muestra cambiando el estilo
    "cookiebot": """<div id="CybotCookiebotDialog" data-banner style="display:none">Este sitio usa cookies.
      %s %s</div><script>setTimeout(() => CybotCookiebotDialog.style.display = 'block', 300)</script>""" % (
        boton("Usar solo cookies necesarias", "reject", 'id="CybotCookiebotDialogBodyButtonDecline"'),
        boton("Permitir todas", "accept", 'id="CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"')),
    "usercentrics": """<div id="usercentrics-root"></div><script>
      usercentricsRoot = document.getElementById('usercentrics-root').attachShadow({mode: 'open'});
      usercentricsRoot.innerHTML = `<div>Privacidad. %s %s</div>`</script>""" % (
        boton("Aceptar todo", "accept", 'data-testid="uc-accept-all-button"'),
        boton("Denegar", "reject", 'data-testid="uc-deny-all-button"')),
    # una casilla de un panel de cookies no lo descarta como banner
    "generico": """<div class="aviso-cookies" data-banner role="dialog">Utilizamos cookies propias y de terceros.
      <label><input type="checkbox" checked disabled> Necesarias</label>
      %s %s %s</div>""" % (boton("Aceptar todas", "accept"), boton("Configurar", "config"),
                           boton("Rechazar todas", "reject")),
    "iframe": '<iframe src="http://localhost:{port}/banner" width="600" height="200"></iframe>',
    "banner": """<div id="cookie-aviso" data-banner>Cookies. %s %s</div>""" % (
        boton("Aceptar", "accept"), boton("Rechazar", "reject")),
    "solo-aceptar": """<div id="cookie-banner" data-banner>Al navegar aceptas las cookies. %s
      <a href="#">Más información</a></div>""" % boton("Aceptar", "accept"),
    "otro-rechazar": """<form id="solicitud"><p>Solicitud de vacaciones de Ana Gil.</p>%s %s</form>""" % (
        boton("Rechazar", "reject", 'type="button"'), boton("Aprobar", "approve", 'type="button"')),
    # el paso de consentimiento de un trámite: «No acepto» cancelaría la solicitud
    "consentimiento": """<form><fieldset id="consentimiento"><legend>Consentimiento</legend>
      <p>¿Autoriza la consulta de sus datos de identidad?</p>%s %s</fieldset></form>""" % (
        boton("Acepto", "accept", 'type="button"'), boton("No acepto", "reject", 'type="button"')),
    # AEM envuelve la página entera en `cmp-container`: el «Decline» de un OAuth no es de cookies
    "aem": """<div class="cmp-container"><div class="cmp-text">La aplicación Agenda quiere leer tu calendario.</div>
      %s %s</div>""" % (boton("Allow", "accept"), boton("Decline", "reject")),
    # contenedor con «cookie» en la clase y un campo de texto: un formulario, no un banner
    "con-campo": """<div class="cookie-boletin">Recetas de galletas (cookies) cada semana.
      <input name="correo" placeholder="Tu correo"> %s</div>""" % boton("Rechazar", "reject"),
    # contenedor de nombre RGPD sin «cookie» en su texto
    "sin-cookie": """<div class="gdpr-solicitud">Solicitud de supresión de datos de Ana Gil. %s %s</div>""" % (
        boton("Aprobar", "approve"), boton("Rechazar", "reject")),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip("/")
        if name not in PAGES:
            self.send_error(404)
            return
        body = (HEAD.format(t=name) + PAGES[name].replace("{port}", str(self.server.server_address[1]))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def clicks(frame, wait: float) -> list:
    """`window.clic` del frame en cuanto deja de estar vacío, o tras `wait` s."""
    for _ in range(int(wait / 0.1)):
        got = await frame.evaluate("window.clic")
        if got:
            return got
        await asyncio.sleep(0.1)
    return await frame.evaluate("window.clic")


async def consents(session) -> list:
    observe.reset()
    return [f.get("consent") for f in (await observe.observe(session))["frames"]]


async def main(base: str):
    session = await BrowserSession.start()
    try:
        page = session.page
        for name, cmp in (("onetrust", "onetrust"), ("cookiebot", "cookiebot"), ("usercentrics", "usercentrics"),
                          ("generico", "generico")):
            await page.goto(f"{base}/{name}")
            assert await clicks(page, 3) == ["reject"], (name, await page.evaluate("window.clic"))
            await asyncio.sleep(0.5)  # nada más después: ni un segundo clic ni un «aceptar»
            assert await page.evaluate("window.clic") == ["reject"], name
            assert await consents(session) == [f"rechazado ({cmp})"], name

        await page.goto(f"{base}/iframe")
        await asyncio.sleep(0.3)
        inner = next(f for f in page.frames if f is not page.main_frame)
        assert await clicks(inner, 4) == ["reject"], await inner.evaluate("window.clic")
        assert await page.evaluate("window.clic") == []
        assert await consents(session) == [None, "rechazado (generico)"], await consents(session)

        for name in ("solo-aceptar", "otro-rechazar", "consentimiento", "aem", "con-campo", "sin-cookie"):
            await page.goto(f"{base}/{name}")
            assert await clicks(page, 1.5) == [], (name, await page.evaluate("window.clic"))
            assert await consents(session) == [None], name
    finally:
        await session.close()
    print("test_consent OK")


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        asyncio.run(main(f"http://127.0.0.1:{srv.server_address[1]}"))
    finally:
        srv.shutdown()
