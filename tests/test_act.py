"""browser_goto, browser_act, el traspaso y browser_observe(mode=text) por el protocolo:  uv run --project . tests/test_act.py

En la sesión en vivo, como test_session.py (el navegador aparcado fuera de pantalla). El traspaso NO trae nada a la
pantalla: `kwin.summon` y la notificación son dobles que apuntan con qué se les llamó; el de verdad se prueba en
KWin anidada (test_summon_nest.py). El fichero de traspaso y la auditoría van a un XDG_DATA_HOME temporal.

Las páginas del banco (catalogo, facturas con su iframe de otro origen, justificante) las sirve
`bench/verify/serve.py`: facturas en el 8921 que su iframe espera (si ya hay uno escuchando, se usa ese, solo
para leer), las demás en un puerto libre.
"""

import asyncio
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

DATA = tempfile.mkdtemp(prefix="manos-act-")
os.environ["XDG_DATA_HOME"] = DATA
os.environ["BENCH_HOME"] = DATA  # lo que la página de la subida apunte va aquí, no a una ejecución del banco

from mcp import Client

import kwin
import serve
import server
from browser import session as bs

LIVE_FLAG = Path.home() / ".local/share/manos/driving"
LIVE0 = LIVE_FLAG.exists()
ROW = re.compile(r'\[(\d+)\] (\S+) ("(?:[^"\\]|\\.)*")(.*)')
FLAG = Path(DATA) / "manos/driving"


def serve_on(port: int) -> ThreadingHTTPServer | None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), serve.Handler)
    except OSError:
        return None  # ya hay uno: el del banco sirve los mismos ficheros
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def main(base: str):
    # el fichero de traspaso que no se puede mirar cuenta como puesto (falla cerrado)
    assert not bs.driving()
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.parent.chmod(0)
    try:
        assert bs.driving()
    finally:
        FLAG.parent.chmod(0o700)
    assert not bs.driving()
    # traspaso de un servidor muerto: caduca y se borra; vivo, o el de Meta+Shift+B (sin pid), sigue
    dead = await asyncio.create_subprocess_exec("true")
    await dead.wait()
    FLAG.write_text(json.dumps({"reason": "x", "pid": dead.pid}))
    assert not bs.driving() and not FLAG.exists()
    for flag in ({"reason": "x", "pid": os.getpid()},
                 {"reason": "Diego pulsó Meta+Shift+B y tomó el navegador", "ts": 1}):
        FLAG.write_text(json.dumps(flag))
        assert bs.driving(), flag
    FLAG.unlink()

    async with Client(server.mcp) as c:
        async def call(tool, **args):
            return (await c.call_tool(tool, args)).structured_content

        async def refs(find="", **kw):
            ob = await call("browser_observe", find=find, **kw)
            assert ob["ok"], ob
            return {(m[2], json.loads(m[3])): int(m[1]) for m in map(ROW.match, ob["listing"]) if m}

        page = (HERE / "fixtures/act.html").as_uri()
        try:
            o = await call("browser_open", url=page)
            assert o["ok"], o
            # modo mirar: browser_open recoloca solo cuando `visible` cambia, y el domador va fuera mientras se ve (park
            # y domador de mentira: en vivo no se enseña la ventana; lo de verdad lo prueba test_summon_nest)
            s, calls = server._session, []
            async def fake_park():
                calls.append(s.visible)
            async def fake_tamer(on):
                calls.append(f"domador {on}")
            s.park, s._tamer = fake_park, fake_tamer
            try:
                for v in (True, True, False, False):
                    assert (await call("browser_open", visible=v))["ok"]
            finally:
                del s.park, s._tamer
            assert calls == [True, "domador False", "domador True", False, "domador True"] and not s.visible, calls
            assert kwin.is_loaded("manostamer")
            assert kwin.window_info(s.window_uuid)["x"] >= bs.PARKED_X
            r = await refs()
            nombre, clave = r[("textbox", "Nombre")], r[("textbox", "Contraseña")]

            # type: reemplaza, devuelve la longitud y nunca el texto
            got = await call("browser_act", action="type", ref=nombre, value="Lucía Pérez")
            assert got["ok"] and got["readback"] == {"len": 11} and "Lucía" not in json.dumps(got, ensure_ascii=False), got
            got = await call("browser_act", action="type", ref=nombre, value="Ana")
            assert got["readback"] == {"len": 3}, got
            # contraseña: ni teclear ni pulsar teclas; el siguiente paso es el traspaso
            for kw in ({"action": "type", "value": "hunter22"}, {"action": "press", "keys": "a"}):
                got = await call("browser_act", ref=clave, **kw)
                assert got["error"]["type"] == "Refused" and "browser_handoff" in got["error"]["next"], got
            assert "hunter22" not in json.dumps(got, ensure_ascii=False)
            got = await call("browser_act", action="select", ref=r[("combobox", "Provincia")], value="Murcia")
            assert got["ok"] and got["readback"] == {"selected": "Murcia"}, got
            acepto = r[("checkbox", "Acepto las condiciones")]
            assert (await call("browser_act", action="check", ref=acepto))["readback"] == {"checked": True}
            assert (await call("browser_act", action="uncheck", ref=acepto))["readback"] == {"checked": False}
            got = await call("browser_act", action="click", ref=r[("button", "Contar")])
            assert got["ok"] and got["changed"] is True and not got["navigated"], got
            got = await call("browser_act", action="press", ref=nombre, keys="NoEsUnaTecla")
            assert got["error"]["type"] == "ValueError", got
            # un botón muerto: la tercera pulsación idéntica sin cambio es Stuck y no se hace
            nada = [await call("browser_act", action="click", ref=r[("button", "Nada")]) for _ in range(3)]
            assert nada[0]["ok"] and nada[1]["ok"] and nada[2]["error"]["type"] == "Stuck", nada
            assert "browser_observe" in nada[2]["error"]["message"], nada[2]
            # press no pasa por la guardia: tres flechas iguales no son Stuck (un caret que se mueve no cambia el texto)
            for _ in range(3):
                got = await call("browser_act", action="press", ref=nombre, keys="ArrowLeft")
                assert got["ok"], got
            got = await call("browser_act", action="press", ref=nombre, keys="Control+v")  # pegar: el portapapeles de Diego
            assert got["error"]["type"] == "Refused", got
            # un campo que delega el foco en la contraseña: ni type ni press
            for kw in ({"action": "type", "value": "hunter22"}, {"action": "press", "keys": "a"}):
                got = await call("browser_act", ref=r[("textbox", "Usuario")], **kw)
                assert got["error"]["type"] == "Refused" and "secreto" in got["error"]["message"], got
            got = await call("browser_act", action="type", ref=r[("button", "Contar")], value="x")
            assert got["error"]["type"] == "ValueError", got  # type solo en un campo editable
            # el nombre accesible es parte de la identidad: un botón que cambia de texto ya no es el observado
            assert (await call("browser_act", action="click", ref=r[("button", "Mostrar")]))["ok"]
            got = await call("browser_act", action="click", ref=r[("button", "Mostrar")])
            assert got["error"]["type"] == "StaleHandle", got
            # un secreto tachado no se recupera con find, en ningún modo
            server.sanitize.remember(["zq8-Secreto-77"])
            for mode in ("text", "actions"):
                assert not (await call("browser_observe", mode=mode, find="zq8"))["listing"], mode
            assert any("«redactado»" in ln for ln in (await call("browser_observe", mode="text"))["listing"])
            r = await refs()  # observar acuña refs nuevos y retira los de antes
            # el selector de ficheros nativo nunca se abre: el clic en el input lo para el oyente de la sesión
            before = server._session.choosers
            assert (await call("browser_act", action="click", ref=r[("button", "Adjunto")]))["ok"]
            assert server._session.choosers == before + 1, server._session.choosers
            # un diálogo cerrado por fuera de browser_dialog (Diego en su ventana) deja de bloquear
            got = await call("browser_act", action="click", ref=r[("button", "Avisar")])
            assert got["ok"] and got["dialog"] == "alert", got
            assert (await call("browser_observe"))["error"]["type"] == "DialogOpen"
            await (await server._session.cdp()).send("Page.handleJavaScriptDialog", {"accept": True})
            for _ in range(50):
                if server._session.dialog is None:
                    break
                await asyncio.sleep(0.05)
            assert server._session.dialog is None
            r = await refs()

            # upload: un fichero real; uno que no existe y uno del perfil del navegador, no
            with tempfile.NamedTemporaryFile(suffix=".txt") as f:
                f.write(b"hola")
                f.flush()
                adj = r[("button", "Adjunto")]
                got = await call("browser_act", action="upload", ref=adj, files=[f.name])
                assert got["ok"] and got["readback"] == {"files": 1}, got
            got = await call("browser_act", action="upload", ref=adj, files=["/no/existe.pdf"])
            assert got["error"]["type"] == "ValueError" and "no existe" in got["error"]["message"], got
            # rutas vedadas: el perfil del navegador, cualquier carpeta con punto bajo la casa (también por un enlace),
            # y una relativa
            hidden = Path.home() / ".config/kwinrc"  # existe; no se lee: se niega antes
            with tempfile.TemporaryDirectory() as d:
                link = Path(d) / "inocente.txt"
                link.symlink_to(hidden)
                for bad in (bs.PROFILE / "Default/Preferences", hidden, link):
                    got = await call("browser_act", action="upload", ref=adj, files=[str(bad)])
                    assert got["error"]["type"] == "PathRefused" and "pídeselo a Diego" in got["error"]["next"], got
                got = await call("browser_act", action="upload", ref=adj, files=["fixtures/act.html"])
                assert got["error"]["type"] == "ValueError" and "absoluta" in got["error"]["message"], got
                # la misma puerta en form_fill: el campo de ficheros se queda sin tocar
                d_ = await call("form_describe")
                fid = next(f["i"] for f in d_["fields"] if f.get("k") == "file")
                ff = await call("form_fill", fields=[{"id": fid, "value": str(link)}])
                assert ff["ok"] and any("vedada" in b[1] for b in ff["blocked"]), ff
            # y en file://: el modo texto leería el fichero
            for tool in ("browser_goto", "browser_open"):
                got = await call(tool, url=hidden.as_uri())
                assert got["error"]["type"] == "PathRefused", (tool, got)
            r = await refs()

            # el texto de la página: filas de tabla con « | », sin el valor de ningún campo
            t = await call("browser_observe", mode="text")
            assert t["ok"] and "# Pruebas de browser_act" in t["listing"] and "A-1 | 3,50 €" in t["listing"], t
            assert "Clics: 1" in t["listing"] and "Ana" not in json.dumps(t, ensure_ascii=False) and t["observation_id"], t
            assert (await call("browser_observe", mode="text", find="clics"))["listing"] == ["Clics: 1"]

            # upload sobre un botón que no abre ningún selector
            got = await call("browser_act", action="upload", ref=r[("button", "Contar")], files=[str(HERE / "fixtures/act2.html")])
            assert got["error"]["type"] == "ValueError" and "selector" in got["error"]["message"], got

            # y un enlace dentro de una página file:// (la ruta de context.route, no la de las tools)
            with tempfile.TemporaryDirectory() as d:
                leak = Path(d) / "enlaces.html"
                leak.write_text(f'<!doctype html><title>enlaces</title><a href="{hidden.as_uri()}">secreto</a>'
                                f'<a href="{Path(d).as_uri()}/">carpeta</a>')
                assert (await call("browser_goto", url=leak.as_uri()))["ok"]
                for name in ("secreto", "carpeta"):
                    got = await call("browser_act", action="click", ref=(await refs())[("link", name)])
                    assert got["ok"] and "kwinrc" not in got["url"], got
                    t = json.dumps(await call("browser_observe", mode="text"), ensure_ascii=False)
                    # la carga se aborta: la página de error de Chrome, ni el fichero ni el listado de la carpeta
                    assert "ERR_ACCESS_DENIED" in t and "Index of" not in t and "[Windows]" not in t, t
                    await call("browser_goto", url=leak.as_uri())
                assert (await call("browser_goto", url=page))["ok"]
            r = await refs()
            # navegar mata los refs, aunque la página nueva tenga un nodo en el mismo xpath
            got = await call("browser_act", action="click", ref=r[("link", "Siguiente página")])
            assert got["ok"] and got["navigated"] and got["url"].endswith("act2.html"), got
            got = await call("browser_act", action="type", ref=r[("textbox", "Nombre")], value="x")
            assert got["error"]["type"] == "StaleHandle", got  # mismo xpath en act2.html, otro mundo
            g = await call("browser_goto", action="back")
            assert g["ok"] and g["url"].endswith("act.html") and g["title"] == "Acciones", g
            g = await call("browser_goto", action="forward")
            assert g["ok"] and g["url"].endswith("act2.html"), g
            assert (await call("browser_goto", url=page, action="back"))["error"]["type"] == "ValueError"
            r2 = await refs()
            g = await call("browser_goto", url=page)
            assert g["ok"] and g["url"].endswith("act.html"), g
            got = await call("browser_act", action="click", ref=r2[("link", "Volver")])
            assert got["error"]["type"] == "StaleHandle", got  # el ref de antes de goto

            # Meta+Shift+B a media tanda de form_fill: no se escribe ni un campo más
            d_ = await call("form_describe")
            ids = {f["label"]: f["i"] for f in d_["fields"]}
            filled, real_fill = [], server.forms._fill_one

            async def fill_then_flag(*a, **kw):
                filled.append(1)
                out = await real_fill(*a, **kw)
                FLAG.parent.mkdir(parents=True, exist_ok=True)
                FLAG.write_text('{"reason": "Diego pulsó Meta+Shift+B y tomó el navegador"}')
                return out
            server.forms._fill_one = fill_then_flag
            try:
                ff = await call("form_fill", fields=[{"id": ids["Nombre"], "value": "Eva"},
                                                     {"id": ids["Provincia"], "value": "Madrid"}])
            finally:
                server.forms._fill_one = real_fill
                FLAG.unlink(missing_ok=True)
            assert ff["error"]["type"] == "UserDriving" and len(filled) == 1, (ff, filled)

            # traspaso: dobles de summon y de la notificación; el fichero es el estado
            calls = []
            real_summon, real_notify = kwin.summon, server._notify
            kwin.summon = lambda cls, activate=False: calls.append(("summon", cls, activate)) or {}
            server._notify = lambda *a: calls.append(("notify", *a))
            try:
                h = await call("browser_handoff", reason="Inicia sesión en el portal")
                assert h["ok"] and FLAG.exists(), h
                assert calls[0] == ("summon", "ManosBrowser", False) and calls[1][0] == "notify", calls  # sin activar
                assert "Inicia sesión" in calls[1][2] and json.loads(FLAG.read_text())["reason"] == "Inicia sesión en el portal"
                for tool, args in [("browser_observe", {}), ("browser_observe", {"mode": "text"}),
                                   ("browser_act", {"action": "click", "ref": 1}), ("browser_goto", {"url": page}),
                                   ("form_describe", {}), ("browser_dialog", {}), ("browser_handoff", {"reason": "x"}),
                                   ("screen_capture", {"target": "screen"}), ("browser_open", {"url": page}),
                                   ("browser_close", {})]:
                    got = await call(tool, **args)
                    assert got["error"]["type"] == "UserDriving", (tool, got)
                    assert got["error"]["next"] == server.NEXT["UserDriving"] and "browser_resume" in got["error"]["next"]
                    assert "portal" in got["error"]["message"], got  # el motivo, para que el modelo se lo diga a Diego
                assert len(calls) == 2, calls
                # Diego sigue en la ventana: no se recupera, y el fichero se queda
                real_active, kwin.active = kwin.active, lambda: server._session.window_uuid
                try:
                    got = await call("browser_resume")
                finally:
                    kwin.active = real_active
                assert got["error"]["type"] == "BrowserFocused" and FLAG.exists(), got
            finally:
                kwin.summon, server._notify = real_summon, real_notify
            res = await call("browser_resume")
            assert res["ok"] and not FLAG.exists() and res["url"].endswith("act.html"), (
                res, server._session.window_uuid, kwin.window_info(kwin.active() or "") if kwin.active() else None)
            info = kwin.window_info(server._session.window_uuid)
            assert info["x"] >= bs.PARKED_X, info  # aparcada otra vez
            assert not server._session._handed and kwin.is_loaded("manostamer"), "take_back no repuso el domador"
            # Meta+Shift+B (otro proceso: solo el fichero): la sesión lo nota sola y suelta el selector y el domador
            FLAG.write_text('{"reason": "Diego pulsó Meta+Shift+B y tomó el navegador"}')
            for _ in range(40):  # `_handed` cambia antes de que acabe la descarga del domador: esperar a los dos
                if server._session._handed and not kwin.is_loaded("manostamer"):
                    break
                await asyncio.sleep(0.1)
            w = server._session._watch
            assert server._session._handed, ("la sesión no notó el traspaso", w, w.done() and (w.cancelled() or w.exception()))
            assert not kwin.is_loaded("manostamer"), "el domador sigue cargado"
            res = await call("browser_resume")
            assert res["ok"] and not FLAG.exists() and not server._session._handed and kwin.is_loaded("manostamer"), res
            assert (await call("browser_observe"))["ok"]

            # las páginas del banco: una tabla paginada, una tabla en un iframe de otro origen, subir tras un botón
            await call("browser_goto", url=f"{base}/catalogo.html")
            t = await call("browser_observe", mode="text", find="CAT-1001")
            assert t["listing"] and re.fullmatch(r"CAT-1001 \| Tirador aluminio 10 mm \| 4,21\s€", t["listing"][0]), t
            sig = (await refs("Siguiente"))[("button", "Siguiente")]
            got = await call("browser_act", action="click", ref=sig)
            assert got["changed"] is True, got
            t = await call("browser_observe", mode="text", find="Página")
            assert any("Página 2 de 12" in ln for ln in t["listing"]), t

            await call("browser_goto", url="http://127.0.0.1:8921/facturas.html")
            full = await call("browser_observe", mode="text", viewport_only=False)
            assert "(iframe: localhost:8921)" in full["listing"], full["listing"][:5]
            last = next(ln for ln in full["listing"] if ln.startswith("F-2026-0462"))
            assert "Panadería Hermanos Gil" in last and "|" in last, last
            assert any(f["frame"] == "localhost:8921" and f["n"] for f in full["frames"]), full["frames"]
            seen = await call("browser_observe", mode="text", find="F-2026-0462")
            assert not seen["listing"], seen  # fuera del viewport del visor
            ref462 = (await refs("F-2026-0462", viewport_only=False))[("link", "F-2026-0462")]
            got = await call("browser_act", action="scroll", ref=ref462)
            assert got["ok"], got
            seen = await call("browser_observe", mode="text", find="F-2026-0462")
            assert seen["listing"] and "Panadería" in seen["listing"][0], seen  # se desplazó dentro del iframe

            await call("browser_goto", url=f"{base}/justificante.html")
            r = await refs()
            with tempfile.TemporaryDirectory() as d:
                pdf = Path(d) / "pago.pdf"
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                got = await call("browser_act", action="upload", ref=r[("button", "Subir justificante")], files=[str(pdf)])
                assert got["ok"] and got["readback"] == {"files": 1} and got["changed"], got
            t = await call("browser_observe", mode="text", find="pago.pdf")
            assert t["listing"] and "Solo imágenes" in json.dumps(await call("browser_observe", mode="text"), ensure_ascii=False), t
        finally:
            await call("browser_close")

    text = (Path(DATA) / "manos/audit.jsonl").read_text()
    acts = [l for l in map(json.loads, text.splitlines()) if l["tool"] == "browser_act"]
    assert acts and all(l["rung"] == "web" and l["acting"] for l in acts), acts
    assert "Lucía" not in text and "hunter22" not in text and "act.html" not in text, "la auditoría lleva valores"
    tools = {l["tool"] for l in map(json.loads, text.splitlines())}
    assert {"browser_goto", "browser_handoff", "browser_resume"} <= tools, tools


own = serve_on(8921)  # el iframe de facturas.html apunta a localhost:8921
other = serve_on(0)
try:
    asyncio.run(main(f"http://127.0.0.1:{other.server_address[1]}"))
finally:
    for srv in (own, other):
        if srv:
            srv.shutdown()
    shutil.rmtree(DATA, ignore_errors=True)
assert LIVE_FLAG.exists() == LIVE0, "tocó el fichero de traspaso en vivo"
print("test_act OK")
