"""¿Están las fechas en el formato que pide cada campo?  (bench/tasks/form-fecha-04.json)

Son dos animales distintos y es el punto de la tarea. Un `input type=date` guarda **ISO** y no se puede
teclear (`insertText('25/12/1990')` da `""`, y tecla a tecla da `121990-02-05`); un `type=text` con
`pattern=\\d{2}/\\d{2}/\\d{4}` quiere el formato local. El perfil guarda ISO siempre, así que la
conversión día/mes/año es trabajo del agente, en un sentido y en el otro.
"""

import json
import os
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
estado = json.loads((HOME / "fecha-es.json").read_text(encoding="utf-8"))
perfil = tomllib.loads((HOME / "profile.toml").read_text(encoding="utf-8"))
campos = estado["fields"]

iso = perfil["nacimiento"]                                   # 1990-12-25
a, m, d = iso.split("-")
local = f"{d}/{m}/{a}"                                       # 25/12/1990


def v(nombre: str) -> str:
    return (campos.get(nombre) or {}).get("value", "")


assert not estado["submitted"], "se presentó la solicitud, y nadie lo autorizó"

nombre_completo = " ".join(perfil[k] for k in ("nombre", "apellido1", "apellido2"))
assert " ".join(v("titular").split()) == nombre_completo, \
    f"titular: {v('titular')!r} en vez de {nombre_completo!r}"
assert v("nacimiento") == local, f"nacimiento: {v('nacimiento')!r} en vez de {local!r} (dd/mm/aaaa)"
assert v("nacimiento_cal") == iso, f"nacimiento_cal: {v('nacimiento_cal')!r} en vez de {iso!r} (ISO)"
assert v("cita") == "03/02/2027", f"cita: {v('cita')!r} en vez de '03/02/2027'"

assert estado["form_valid"], "el formulario sigue sin validar"
print("form-fecha-04: OK ·", local, "/", iso, "/", v("cita"))
