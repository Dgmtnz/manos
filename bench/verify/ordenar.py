"""¿Quedó Descargas ordenada por extensión, moviendo los ficheros con Dolphin?  (dolphin-ordenar-18)

Lo que vería una persona en Dolphin: dentro de Descargas solo las carpetas PDF, Imágenes y Textos, cada una con
sus ficheros, nada suelto y ninguna «New Folder» de un intento a medias. La trampa es `informe.pdf.txt`: su
extensión es .txt y va a Textos (el filtro de Dolphin con «*.pdf» también lo selecciona). Cada fichero tiene que
ser el mismo de antes, movido y no copiado: mismo inodo (el setup los apunta en `$XDG_DATA_HOME/bench/inodos.txt`,
fuera del alcance del agente) y mismo contenido. Con auditoría, que las carpetas salieron de create_dir de
Dolphin y que hubo al menos una acción de mover o pegar en Dolphin.
"""

import os
import unicodedata
from pathlib import Path

from comun import HOME, auditoria

QUIERO = {
    "PDF": {"factura-enero.pdf", "factura-febrero.pdf", "contrato-alquiler.pdf"},
    "Imágenes": {"foto-playa.jpg", "captura.png"},
    "Textos": {"notas.txt", "lista-compra.txt", "informe.pdf.txt"},
}
MOVER = {"move_to_inactive_split_view", "edit_paste", "edit_cut", "renamefile"}

nfc = lambda s: unicodedata.normalize("NFC", s)
raiz = HOME / "Descargas"
assert raiz.is_dir(), "no está la carpeta Descargas"
dentro = {nfc(p.name): p for p in raiz.iterdir()}
assert set(dentro) == set(QUIERO), f"en Descargas hay {sorted(dentro)} en vez de solo {sorted(QUIERO)}"
inodos = dict(l.split(" ", 1)[::-1] for l in (Path(os.environ["XDG_DATA_HOME"]) / "bench/inodos.txt")
              .read_text(encoding="utf-8").splitlines())
for carpeta, ficheros in QUIERO.items():
    d = dentro[carpeta]
    assert d.is_dir(), f"{carpeta} no es una carpeta"
    hay = {nfc(p.name) for p in d.iterdir()}
    assert hay == ficheros, f"{carpeta} tiene {sorted(hay)} en vez de {sorted(ficheros)}"
    for f in ficheros:
        p = d / f
        assert p.is_file(), f"{carpeta}/{f} no es un fichero"
        assert p.read_text(encoding="utf-8") == f + "\n", f"{carpeta}/{f} no tiene su contenido original"
        assert str(p.stat().st_ino) == inodos[f], f"{carpeta}/{f} es una copia, no el fichero movido"

pasos = auditoria()
if pasos is not None:
    en_dolphin = [p for p in pasos if p["ok"] and p["resource_class"] == "org.kde.dolphin" and p["tool"] == "app_invoke"]
    creadas = sum(p["action"] in ("create_dir", "create_subdir") for p in en_dolphin)
    assert creadas >= 3, f"solo {creadas} create_dir en Dolphin: las carpetas no salieron de Dolphin"
    assert any(p["action"] in MOVER for p in en_dolphin), "ninguna acción de mover o pegar en Dolphin"

print("dolphin-ordenar-18: OK")
