"""¿Hay una carpeta «Facturas 2026», vacía, creada con Dolphin?  (bench/tasks/dolphin-carpeta-08.json)

Lo que miraría una persona en Dolphin: la carpeta con ese nombre exacto, vacía, y ninguna «Nueva carpeta»
olvidada de un intento a medias. Con auditoría, que salió de la acción `create_dir` de Dolphin (peldaño 1;
el nombre se pone en su diálogo). Las tools de ficheros del agente no crean carpetas vacías, así que una
carpeta vacía ya dice mucho incluso sin auditoría.
"""

import re

from comun import HOME, auditoria

carpeta = HOME / "Facturas 2026"
assert carpeta.is_dir(), f"no hay ninguna carpeta {carpeta.name!r} en el directorio de trabajo"
dentro = [p.name for p in carpeta.iterdir()]
assert not dentro, f"la carpeta tenía que quedar vacía y tiene {dentro}"
sobran = [p.name for p in HOME.iterdir() if p.is_dir() and re.match(r"(nueva carpeta|new folder)", p.name, re.I)]
assert not sobran, f"quedaron carpetas de un intento a medias: {sobran}"

pasos = auditoria()
if pasos is not None:
    assert any(p["tool"] == "app_invoke" and p["ok"] and p["action"] == "create_dir"
               and p["resource_class"] == "org.kde.dolphin" for p in pasos), \
        "la carpeta no salió de la acción create_dir de Dolphin"

print("dolphin-carpeta-08: OK")
