"""ask_human por el protocolo (`mcp.Client` en memoria, sin escritorio ni navegador):  uv run --project . tests/test_ask.py

Con los dos transportes del SDK (`legacy`: elicitation/create a mitad de llamada; `auto`: 2026-07-28,
InputRequiredResult y reintento). Un cliente con elicitation contesta choice, text y confirm; rechazar o cancelar dan `NoHuman`;
un cliente sin la capacidad da `NoHuman` en el acto (como `claude -p` sin nadie delante); una pregunta que nombra un
secreto da `Refused` sin llegar a preguntar; y la auditoría lleva `acting: false` sin el texto de la pregunta.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA = tempfile.mkdtemp(prefix="manos-ask-")
os.environ["XDG_DATA_HOME"] = DATA

from mcp import Client
from mcp_types import ElicitResult

import server

asked = []
reply = {}


async def human(ctx, params):
    asked.append(params)
    return ElicitResult(action=reply["action"], content=reply.get("content"))


async def main(mode):
    async with Client(server.mcp, mode=mode, elicitation_callback=human) as c:
        tool = next(t for t in (await c.list_tools()).tools if t.name == "ask_human")
        assert set(tool.input_schema["properties"]) == {"question", "kind", "options"} and tool.annotations.read_only_hint, tool.input_schema

        async def ask(**args):
            return (await c.call_tool("ask_human", args)).structured_content

        reply.update(action="accept", content={"respuesta": "informe-v2.odt"})
        r = await ask(question="¿Cuál de los dos guardo?", options=["informe-v1.odt", "informe-v2.odt"])
        assert r == {"ok": True, "kind": "choice", "answer": "informe-v2.odt", "error": None}, r
        schema = asked[-1].requested_schema["properties"]["respuesta"]
        assert schema["enum"] == ["informe-v1.odt", "informe-v2.odt"] and asked[-1].message == "¿Cuál de los dos guardo?", schema
        reply.update(action="accept", content={"respuesta": "Para el viernes"})
        r = await ask(question="¿Qué plazo pongo?", kind="text")
        assert r["ok"] and r["answer"] == "Para el viernes", r
        reply.update(action="accept", content={"respuesta": True})
        r = await ask(question="¿Sigo con los 12 ficheros?", kind="confirm")
        assert r["ok"] and r["answer"] is True, r

        for rep in ({"action": "decline"}, {"action": "cancel"}):
            reply.clear()
            reply.update(rep)
            r = await ask(question="¿Sigo?", kind="confirm")
            assert r["error"]["type"] == "NoHuman" and "respuesta final" in r["error"]["next"], (rep, r)

        n = len(asked)
        for q in ("¿Cuál es tu contraseña del banco?", "Dime el código de verificación del SMS", "¿Qué PIN pongo?",
                  "¿Me pasas el código del SMS?", "Necesito el código 2FA", "¿Cuál es el número de tu tarjeta?",
                  "Los 16 dígitos de la Visa", "¿Y el passcode?", "¿Cuál es la pass del wifi?"):
            r = await ask(question=q, kind="text")
            assert r["error"]["type"] == "Refused" and "browser_handoff" in r["error"]["next"], r
        for bad in ({"question": "¿Cuál?"}, {"question": "¿Cuál?", "options": [str(i) for i in range(9)]},
                    {"question": "x" * 501, "kind": "text"}, {"question": "¿Sigo?", "kind": "confirm", "options": ["a", "b"]}):
            r = await ask(**bad)
            assert r["error"]["type"] == "ValueError", (bad, r)
        assert len(asked) == n, "preguntó algo que debía negarse antes"

    async with Client(server.mcp, mode=mode) as c:  # sin elicitation: falla en el acto, no espera a nadie
        t0 = time.perf_counter()
        r = (await c.call_tool("ask_human", {"question": "¿Sigo?", "kind": "confirm"})).structured_content
        ms = (time.perf_counter() - t0) * 1000
        assert r["error"]["type"] == "NoHuman" and ms < 1000, (r, ms)
        print(f"{mode}: sin elicitation, NoHuman en {ms:.0f} ms")

    lines = [json.loads(l) for l in (Path(DATA) / "manos/audit.jsonl").read_text().splitlines()]
    assert lines and all(l["tool"] == "ask_human" and l["acting"] is False and l["rung"] == "human" for l in lines), lines
    text = json.dumps(lines, ensure_ascii=False)
    assert "guardo" not in text and "informe" not in text and "viernes" not in text and "contraseña" not in text, text


try:
    for m in ("legacy", "auto"):  # elicitation/create a mitad de llamada, e InputRequiredResult (2026-07-28)
        asyncio.run(main(m))
finally:
    shutil.rmtree(DATA, ignore_errors=True)
print("test_ask OK")
