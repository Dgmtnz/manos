"""¿Se rellenaron los campos que viven dentro del shadow root?  (bench/tasks/form-shadow-05.json)

El titular y el IBAN están dentro de un componente con shadow root abierto. Dos cosas que se comprueban
aquí y en ningún otro sitio: que se alcanzaron (la aserción de foco falla si se compara
`document.activeElement`, que es el HOST y no el campo), y que son **válidos** uno a uno — la asociación
de formulario no cruza la frontera del shadow root, así que `form.checkValidity()` del formulario de
arriba no los mira y un `form_valid` global no prueba nada sobre ellos.
"""

import json
import os
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
estado = json.loads((HOME / "shadow.json").read_text(encoding="utf-8"))
perfil = tomllib.loads((HOME / "profile.toml").read_text(encoding="utf-8"))
campos = estado["fields"]

assert not estado["submitted"], "se pidió la devolución, y nadie lo autorizó"

nombre_completo = " ".join(perfil[k] for k in ("nombre", "apellido1", "apellido2"))
esperado = {"email": perfil["email"], "titular": nombre_completo, "iban": perfil["iban"]}
for nombre, quiero in esperado.items():
    campo = campos.get(nombre) or {}
    puesto = " ".join(campo.get("value", "").split())
    assert puesto == quiero, f"{nombre}: {puesto!r} en vez de {quiero!r}"
    assert campo.get("valid", True), f"{nombre}: inválido — {campo.get('msg')}"

print("form-shadow-05: OK · titular e IBAN dentro del shadow root")
