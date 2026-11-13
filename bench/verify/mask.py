"""¿Entró el dato en los tres campos con máscara?  (bench/tasks/form-mask-02.json)

Las máscaras no rompen el tecleo, rompen la verificación: la relectura **nunca** es igual a lo que se
escribió (`600112233` sale como `600 11 22 33`, `123456` como `1.234,56 €`). Así que se compara
normalizado, exactamente como lo hace el bucle del servidor. Lo que sería un fallo de verdad es una
relectura **corta**: ahí la máscara rechazó caracteres y el dato está a medias.
"""

import json
import os
import re
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
estado = json.loads((HOME / "mask.json").read_text(encoding="utf-8"))
perfil = tomllib.loads((HOME / "profile.toml").read_text(encoding="utf-8"))
campos = estado["fields"]

digitos = lambda s: re.sub(r"\D", "", s)


def v(nombre: str) -> str:
    return (campos.get(nombre) or {}).get("value", "")


assert not estado["submitted"], "se domicilió el recibo, y nadie lo autorizó"

# El teléfono del perfil lleva +34 y la máscara solo deja nueve dígitos: se compara el número nacional.
assert digitos(v("telefono")) == digitos(perfil["telefono"])[-9:], \
    f"telefono: {v('telefono')!r} no normaliza a {perfil['telefono']!r}"
assert digitos(v("importe")) == "123456", f"importe: {v('importe')!r} no son 1234,56 €"
assert v("dni") == perfil["dni"].upper(), f"dni: {v('dni')!r} en vez de {perfil['dni'].upper()!r}"

for nombre in ("telefono", "importe", "dni"):
    assert (campos.get(nombre) or {}).get("valid", True), \
        f"{nombre}: el navegador lo da por inválido — {(campos[nombre]).get('msg')}"

assert estado["form_valid"], "el formulario sigue sin validar"
print("form-mask-02: OK ·", {k: v(k) for k in ("telefono", "importe", "dni")})
