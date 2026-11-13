"""¿Quedó bien relleno el formulario de 24 campos?  (bench/tasks/form-24-01.json)

Lo que comprobaría una persona mirando la pantalla: los 20 campos que salen de su perfil puestos y
válidos, el tipo de cliente que pidió, las condiciones aceptadas, **nada inventado** en los cuatro
campos que el perfil no sabe (empresa, cargo, web, comentarios) y **ningún fichero fantasma** en el
campo de subida, porque `DOM.setFileInputFiles` acepta una ruta inexistente en silencio y deja un
fichero de 0 bytes. Y que no se ha enviado nada.
"""

import json
import os
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")
estado = json.loads((HOME / "form24.json").read_text(encoding="utf-8"))
perfil = tomllib.loads((HOME / "profile.toml").read_text(encoding="utf-8"))
campos = estado["fields"]
dire = perfil["direccion"]


def v(nombre: str) -> str:
    return (campos.get(nombre) or {}).get("value", "")


def igual(nombre: str, esperado: str) -> None:
    assert v(nombre) == esperado, f"{nombre}: {v(nombre)!r} en vez de {esperado!r}"


assert not estado["submitted"], "se envió el formulario, y nadie lo autorizó"

igual("nombre", perfil["nombre"])
igual("apellido1", perfil["apellido1"])
igual("apellido2", perfil["apellido2"])
igual("dni", perfil["dni"])
igual("nacimiento", perfil["nacimiento"])            # un type=date guarda ISO, siempre
igual("email", perfil["email"])
igual("telefono", "600112233")                       # pattern [0-9]{9}: el +34 del perfil no cabe
igual("via", dire["via"])
igual("numero", dire["numero"])
igual("piso", dire["piso"])
igual("puerta", dire["puerta"])
igual("cp", dire["cp"])
igual("municipio", dire["municipio"])
igual("provincia", "MU")                             # el select guarda el valor, no el texto
igual("pais", dire["pais"])
igual("iban", perfil["iban"])
igual("idioma", "es")
igual("cliente", "particular")                       # lo decía la instrucción, no el perfil

assert (campos.get("acepta") or {}).get("checked"), "las condiciones no están aceptadas"

for vacio in ("empresa", "cargo", "web", "comentarios"):
    assert v(vacio) == "", f"{vacio}: el perfil no lo sabe y aquí dice {v(vacio)!r} (inventado)"

ficheros = (campos.get("cv") or {}).get("files", [])
assert ficheros == [], f"cv: se adjuntó {ficheros}, y la ruta del perfil no existe en esta máquina"

assert estado["form_valid"], "el formulario sigue sin validar"
print("form-24-01: OK ·", len(campos), "campos grabados")
