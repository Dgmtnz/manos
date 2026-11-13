"""¿Sumó en KCalc las facturas válidas y dejó el resumen escrito desde KWrite?  (facturas-kcalc-16)

Lo que miraría una persona: `resumen.txt` dice exactamente «Facturas sumadas: 5» y «Total: 792,45 €». La
F-2026-103 está anulada (la sustituye la 106): sumarla da 6 facturas y 1102,45 €, y eso es un fallo. La
respuesta final también da el total. Con auditoría, que la cuenta salió de los botones de KCalc (al menos 25
pulsaciones por AT-SPI: las cinco cifras con sus decimales, los «+» y el «=» son unas 31) y que el resumen se
escribió y se guardó desde KWrite, no con las tools de ficheros.
"""

import re

from comun import HOME, auditoria, respuesta

TEXTO = "Facturas sumadas: 5\nTotal: 792,45 €"

resumen = HOME / "resumen.txt"
assert resumen.is_file(), "no hay resumen.txt en el directorio de trabajo"
leido = resumen.read_text(encoding="utf-8")
assert leido.removesuffix("\n") == TEXTO, f"resumen.txt dice {leido!r} en vez de {TEXTO!r}"

txt = respuesta()
assert re.search(r"(?<![\d.,])792[.,]45(?!\d)", txt), f"la respuesta no da el total 792,45: {txt[:300]!r}"

pasos = auditoria()
if pasos is not None:
    pulsados = sum(p["tool"] == "ui_act" and p["ok"] and p["rung"] == "2" and p["action"] == "press"
                   and p["resource_class"] == "org.kde.kcalc" for p in pasos)
    assert pulsados >= 25, f"solo {pulsados} pulsaciones por AT-SPI en KCalc: la suma no salió de sus botones"
    en_kwrite = [p for p in pasos if p["ok"] and p["resource_class"] in ("org.kde.kwrite", "org.kde.kate")]
    assert any(p["tool"] == "ui_act" and p["action"] in ("set_text", "type") for p in en_kwrite), \
        "el resumen no entró por KWrite: el fichero se escribió por otro lado"
    assert any((p["tool"] == "app_invoke" and (p["action"] or "").startswith("file_save"))
               or (p["tool"] == "ui_act" and p["action"] in ("press", "key")) for p in en_kwrite), \
        "el resumen no se guardó desde KWrite"
    print(f"(pulsaciones en KCalc: {pulsados})")

print("facturas-kcalc-16: OK · 792,45 €")
