"""La caché de repetición por el protocolo:  uv run --project . tests/test_replay.py

En la sesión en vivo, con el navegador aparcado (como test_act.py); datos en un XDG_DATA_HOME temporal. Una tarea
certificada se guarda (0600 en una carpeta 0700) y task_checkpoint la ofrece; task_replay la repite entera y, otra
vez igual, no se duplica. Un botón renombrado para en `stopped_at` con el motivo; un campo secreto con valor escrito no
guarda nada; lo que compromete (form_submit, Enter, aceptar un diálogo, un clic que navegó, abrió un diálogo o
parece enviar) se para ANTES y no se hace; un `ui_act` corta. Qué mancha la tanda y cuándo empieza otra.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

DATA = tempfile.mkdtemp(prefix="manos-replay-")
os.environ["XDG_DATA_HOME"] = DATA

from mcp import Client

import replay
import server

ROW = re.compile(r'\[(\d+)\] (\S+) ("(?:[^"\\]|\\.)*")(.*)')
DIR = Path(DATA) / "manos/replay"
SITE = Path(DATA) / "sitio"
PAGE = """<!doctype html><html lang="es"><meta charset="utf-8"><title>Repetir</title>
<form action="ok.html">
<label>Nombre <input name="nombre" id="nombre"></label>
<label for="prov">Provincia</label> <select id="prov" name="prov"><option value="">—</option>
<option value="M">Madrid</option><option value="MU">Murcia</option></select>
<label>IBAN <input name="iban" id="iban"></label>
<button type="submit">Enviar solicitud</button>
</form>
<form onsubmit="event.preventDefault(); this.querySelector('button').textContent = 'Tramitado'">
<button type="submit">Tramitar</button>
</form>
<p id="clics">Clics: 0</p>
<button type="button" id="contar">{boton}</button>
<button type="button" id="avisar">Avisar</button>
<button type="button" id="confirmar">Confirmar envío</button>
<script>
let n = 0;
document.getElementById('contar').addEventListener('click', () => {{
  document.getElementById('clics').textContent = 'Clics: ' + (++n);
}});
document.getElementById('avisar').addEventListener('click', () => alert('hola'));
document.getElementById('confirmar').addEventListener('click', e => {{ e.target.textContent = 'Enviado'; }});
</script>
</html>"""
IBAN = "ES9121000418450200051332"


async def main():
    SITE.mkdir()
    page = SITE / "repetir.html"
    page.write_text(PAGE.format(boton="Contar"))
    (SITE / "ok.html").write_text('<!doctype html><meta charset="utf-8"><title>Solicitud registrada</title>'
                                  '<h1>Solicitud registrada</h1>')
    url = page.as_uri()
    async with Client(server.mcp) as c:
        async def call(tool, **args):
            return (await c.call_tool(tool, args)).structured_content

        async def refs():
            ob = await call("browser_observe")
            assert ob["ok"], ob
            return {(m[2], json.loads(m[3])): int(m[1]) for m in map(ROW.match, ob["listing"]) if m}

        async def fields():
            d = await call("form_describe")
            assert d["ok"], d
            return {f["label"]: f for f in d["fields"]}

        async def certify(task, observation=None):
            ob = observation or await call("browser_observe")
            return await call("submit_task_complete", task=task, evidence={"observation_id": ob["observation_id"]})

        clics = [{"kind": "browser", "check": "text_regex", "value": "Clics: 1"}]
        try:
            # 1. grabar: abrir ANTES del checkpoint (así lo hacen los modelos), rellenar, dos clics y el diálogo
            assert (await call("browser_open", url=url))["ok"]
            t = await call("task_checkpoint", task="alta", plan=["rellenar el alta", "contar", "avisar"], accept=clics)
            assert t["ok"] and "replays" not in t, t
            f = await fields()
            ff = await call("form_fill", fields=[{"id": f["Nombre"]["i"], "value": "Ana"},
                                                 {"id": f["Provincia"]["i"], "value": "Murcia"}])
            assert ff["ok"], ff
            r = await refs()
            assert (await call("browser_act", action="click", ref=r[("button", "Contar")]))["ok"]
            got = await call("browser_act", action="click", ref=r[("button", "Avisar")])
            assert got["ok"] and got["dialog"] == "alert", got
            assert (await call("browser_dialog", accept=True))["ok"]
            v = await certify("alta")
            assert v["ok"] and v["replay_saved"] and "alta" in v["replay_reason"], v
            saved = json.loads((DIR / "alta.json").read_text())
            assert [s["tool"] for s in saved["steps"]] == ["browser_open", "form_fill", "browser_act", "browser_act",
                                                           "browser_dialog"], saved["steps"]
            assert saved["steps"][1]["fields"][0] == {"f": {"k": "text", "label": "Nombre", "nm": "nombre",
                                                            "id": "nombre", "section": ""}, "value": "Ana"}, saved
            assert saved["steps"][2]["loc"]["name"] == "Contar" and saved["start"] == url, saved
            assert (DIR / "alta.json").stat().st_mode & 0o777 == 0o600 and DIR.stat().st_mode & 0o777 == 0o700

            # 2. descubrir y repetir: otra tarea con otro nombre la ve en task_checkpoint, con lo literal que escribiría.
            # La repetición rellena y pulsa «Contar», y para ANTES del clic que abrió el diálogo en la vuelta grabada
            assert "alta" in (await call("browser_open", url=url))["next"]  # también al abrir su url
            t = await call("task_checkpoint", task="alta-otra-vez", plan=["lo mismo"], accept=clics)
            assert [x["name"] for x in t["replays"]] == ["alta"] and "task_replay" in t["next"], t
            assert t["replays"][0]["start"] == url and "browser_act×2" in t["replays"][0]["steps"], t
            assert t["replays"][0]["types"] == ["Ana", "Murcia"], t
            rp = await call("task_replay", name="alta")
            assert rp["ok"] and rp["stopped_at"] == 3 and rp["done"] == 3 and "diálogo" in rp["reason"], rp
            assert "ESTA tarea" in rp["next"] and "browser_act(click)" in rp["next"], rp
            assert server._session.dialog is None
            assert "Clics: 1" in (await call("browser_observe", mode="text"))["listing"]
            f = await fields()
            assert f["Nombre"]["len"] == 3 and f["Provincia"]["value"] == "Murcia", f
            v = await certify("alta-otra-vez")
            assert v["ok"] and v["replay_saved"], v
            assert [s["tool"] for s in json.loads((DIR / "alta-otra-vez.json").read_text())["steps"]] == \
                   ["browser_open", "form_fill", "browser_act"]
            # sin nada que comprometa se repite entera, y los mismos pasos no se guardan dos veces
            await call("task_checkpoint", task="alta-3", plan=["lo mismo"], accept=clics)
            rp = await call("task_replay", name="alta-otra-vez")
            assert rp["ok"] and rp["done"] == rp["total"] == 3 and rp["stopped_at"] is None, rp
            assert "submit_task_complete" in rp["next"], rp
            v = await certify("alta-3")
            assert v["ok"] and v["replay_saved"] and "«alta-otra-vez»" in v["replay_reason"], v
            assert sorted(p.name for p in DIR.glob("*.json")) == ["alta-otra-vez.json", "alta.json"]

            # 3. divergencia: el botón se llama de otra manera → para en él, sin pulsar nada
            page.write_text(PAGE.format(boton="Sumar"))
            rp = await call("task_replay", name="alta")
            assert rp["ok"] and rp["stopped_at"] == 2 and rp["done"] == 2, rp
            assert "Contar" in rp["reason"] and "Sumar" in rp["reason"] and "browser_observe" in rp["next"], rp
            assert "Clics: 0" in (await call("browser_observe", mode="text"))["listing"]
            assert (await call("task_replay", name="no-existe"))["error"]["type"] == "NoSuchReplay"

            # 4. un campo secreto con un valor escrito: la tarea se certifica, la repetición no se guarda
            t = await call("task_checkpoint", task="secreto", plan=["iban"],
                           accept=[{"kind": "browser", "check": "text_regex", "value": "Clics: 0"}])
            f = await fields()
            assert f["IBAN"].get("sec") == "B", f["IBAN"]
            assert (await call("form_fill", fields=[{"id": f["IBAN"]["i"], "value": IBAN}]))["ok"]
            v = await certify("secreto")
            assert v["ok"] and v["replay_saved"] is False and "secreto" in v["replay_reason"], v
            assert not (DIR / "secreto.json").exists()

            # 5. un envío irreversible aprobado: se graba, y la repetición para ANTES de él
            assert (await call("browser_open", url=url))["ok"]
            await call("task_checkpoint", task="envio", plan=["enviar"],
                       accept=[{"kind": "browser", "check": "text_regex", "value": "Solicitud registrada"}])
            f = await fields()
            ff = await call("form_fill", fields=[{"id": f["Nombre"]["i"], "value": "Eva"}])
            tok = next(x["token"] for x in ff["submit_tokens"] if x["button"] == "Enviar solicitud")
            assert (await call("form_submit", token=tok))["status"] == "needs_approval"
            sub = await call("form_submit", token=tok, approved=True)
            assert sub["ok"] and sub["status"] != "needs_approval", sub
            v = await certify("envio")
            assert v["ok"] and v["replay_saved"], v
            assert [s["tool"] for s in json.loads((DIR / "envio.json").read_text())["steps"]] == \
                   ["browser_open", "form_fill", "form_submit"]
            rp = await call("task_replay", name="envio")
            assert rp["ok"] and rp["stopped_at"] == 2 and rp["done"] == 2, rp
            assert "visto bueno" in rp["next"] and "approved=true" in rp["next"] and "token=" in rp["next"], rp
            assert "ESTA tarea" in rp["next"], rp
            assert server._session.page.url == url, server._session.page.url  # no se pulsó

            # 6. un clic irreversible sin navegar ni diálogo: el «Confirmar envío» suelto de justificante.html, por su
            # nombre (forms.SEND), y un botón de envío que form_describe clasifica `irreversible` aunque se llame
            # «Tramitar». La vuelta original lo pulsó; la repetición para ANTES y pide comprobar y el visto bueno
            page.write_text(PAGE.format(boton="Contar"))
            for task, button, why in (("confirmar", "Confirmar envío", "parece irreversible"),
                                      ("tramitar", "Tramitar", "envío irreversible")):
                assert (await call("browser_open", url=url))["ok"]
                await call("task_checkpoint", task=task, plan=["pulsar"],
                           accept=[{"kind": "browser", "check": "text_regex", "value": "Enviado|Tramitado"}])
                assert (await call("browser_act", action="click", ref=(await refs())[("button", button)]))["ok"]
                v = await certify(task)
                assert v["ok"] and v["replay_saved"], v
                rp = await call("task_replay", name=task)
                assert rp["ok"] and rp["stopped_at"] == 1 and rp["done"] == 1 and why in rp["reason"], rp
                assert "visto bueno" in rp["next"] and "browser_act(click)" in rp["next"] and button in rp["next"], rp
                text = json.dumps(await call("browser_observe", mode="text"), ensure_ascii=False)
                assert "Enviado" not in text and "Tramitado" not in text, text

            # 7. nunca un paso que compromete, aunque la vuelta grabada lo diera: Enter, aceptar un diálogo, y un
            # form_submit de cualquier clase (aquí `step`, que no pedía visto bueno)
            loc = {"url": url, "xpath": "/html[1]/body[1]/form[1]/label[1]/input[1]", "role": "textbox", "name": "Nombre"}
            for task, step, why in (
                    ("enter", {"tool": "browser_act", "action": "press", "keys": "Enter", "value": "", "files": [],
                               "loc": loc}, "Enter"),
                    ("dialogo", {"tool": "browser_dialog", "accept": True, "text": ""}, "diálogo"),
                    ("paso", {"tool": "form_submit", "button": "Siguiente", "cls": "step", "expect": "any",
                              "approved": False}, "form_submit")):
                replay._steps[:] = [{"tool": "browser_open", "url": url}, step]
                assert replay.take(task, ["x"])[0]
                rp = await call("task_replay", name=task)
                assert rp["ok"] and rp["stopped_at"] == 1 and why in rp["reason"], (task, rp)
            assert "approved" not in rp["next"] and "form_fill para un token" in rp["next"], rp  # `step`: sin visto bueno

            # 8. qué mancha la tanda: un dato del perfil en una etiqueta (el select de provincia de form24 se llama
            # «Provincia * -- elige -- Murcia…») no; un secreto, un dato del perfil que identifica (DNI), una url con
            # credencial o la respuesta escrita de un prompt(), sí
            replay._steps.clear()
            server.forms._profile |= {"municipio": "Murcia", "dni": "12345678Z"}
            server.sanitize.remember(["Murcia", "12345678Z", "zq8-Secreto-77"])
            replay.note({"tool": "browser_open", "url": url, "label": "Provincia * Murcia"})
            replay.note({"tool": "browser_dialog", "accept": False, "text": ""})
            assert not replay._taint
            for step in ({"tool": "browser_open", "url": url, "label": "zq8-Secreto-77"},
                         {"tool": "browser_open", "url": url, "label": "DNI 12345678Z"},
                         {"tool": "browser_open", "url": url + "?code=xyz"},
                         {"tool": "browser_dialog", "accept": False, "text": "hola"}):
                replay.note(step)
                assert replay._taint, step
                replay._taint = ""
            # lo literal de `replays`: sin las referencias al perfil y tachado
            assert replay.typed([{"tool": "form_fill", "fields": [{"f": {}, "value": "{{perfil.nombre}}"},
                                                                  {"f": {}, "value": "Juan"},
                                                                  {"f": {}, "value": "zq8-Secreto-77"}]}]) == \
                ["Juan", "«redactado»"]

            # 9. una tarea nueva empieza tanda: lo de una abandonada no entra, salvo la última navegación; al ponerla
            # al día (sigue abierta) no se toca nada
            replay._steps[:] = [{"tool": "browser_open", "url": url}, {"tool": "browser_act", "action": "click"},
                                {"tool": "browser_goto", "url": url, "action": ""},
                                {"tool": "browser_act", "action": "click"}]
            await call("task_checkpoint", task="nueva", plan=["x"], accept=clics)
            assert replay._steps == [{"tool": "browser_goto", "url": url, "action": ""}], replay._steps
            replay._steps.append({"tool": "browser_act", "action": "click"})
            await call("task_checkpoint", task="nueva", note="sigue")
            assert len(replay._steps) == 2, replay._steps

            # 10. un ui_act corta: se guarda hasta él y la repetición para ahí
            replay._steps.clear()
            replay.note({"tool": "browser_open", "url": url})
            replay.cut("ui_act")
            replay.note({"tool": "browser_goto", "url": "about:blank", "action": ""})
            assert replay.take("corte", ["x"])[0]
            assert [s["tool"] for s in json.loads((DIR / "corte.json").read_text())["steps"]] == ["browser_open", "ui_act"]
            rp = await call("task_replay", name="corte")
            assert rp["stopped_at"] == 1 and "ui_act" in rp["reason"], rp
        finally:
            await call("browser_close")

    every = "".join(p.read_text() for p in DIR.glob("*.json"))
    assert IBAN not in every, "un secreto en la caché"


try:
    asyncio.run(main())
finally:
    shutil.rmtree(DATA, ignore_errors=True)
print("test_replay OK")
