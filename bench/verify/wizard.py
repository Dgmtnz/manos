"""¿Llegó el asistente de tres pasos hasta el final?  (bench/tasks/form-wizard-03.json)

Cada paso solo aparece cuando el anterior **valida**, y sus campos ya existen en el DOM marcados
`hidden` antes de eso. Así que aprobar es: los cuatro campos puestos, el tercer paso visible de verdad
(no solo relleno a ciegas) y el formulario validando. Un agente que rellene los campos ocultos sin
pasar por la puerta dejaría el paso 3 oculto, y eso aquí es un fallo.
"""

import json
import os
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
estado = json.loads((HOME / "wizard.json").read_text(encoding="utf-8"))
perfil = tomllib.loads((HOME / "profile.toml").read_text(encoding="utf-8"))
campos = estado["fields"]

assert not estado["submitted"], "se pidió la cita, y nadie lo autorizó"

for nombre, esperado in (("email", perfil["email"]),
                         ("cp", perfil["direccion"]["cp"]),
                         ("municipio", perfil["direccion"]["municipio"])):
    puesto = (campos.get(nombre) or {}).get("value", "")
    assert puesto == esperado, f"{nombre}: {puesto!r} en vez de {esperado!r}"

assert (campos.get("acepta") or {}).get("checked"), "la confirmación del paso 3 no está marcada"

for paso3 in ("municipio", "acepta"):
    assert not campos[paso3].get("hidden"), f"{paso3}: el paso 3 sigue oculto, no se abrió la puerta"

assert estado["form_valid"], "el formulario sigue sin validar"
print("form-wizard-03: OK · los tres pasos abiertos y válidos")
