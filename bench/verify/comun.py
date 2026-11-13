"""Lo que comparten los verificadores de escritorio: `$BENCH_HOME`, la respuesta final del agente y la auditoría.

- `$BENCH_RESULT`: fichero con el último mensaje del agente. Sin él, las tareas que lo miran fallan.
- `$BENCH_AUDIT`: el `audit.jsonl` de manos de esa ejecución (en la anidada vive en su XDG_DATA_HOME privado).
  Es opcional: sin él no se comprueba el peldaño, y el verificador lo dice. Las líneas anteriores a
  `$BENCH_HOME/.t0` (lo escribe el setup) no cuentan, por si el fichero es compartido.
"""

import json
import os
import unicodedata
from pathlib import Path

HOME = Path(os.environ.get("BENCH_HOME") or Path(__file__).resolve().parents[1] / "run")


def respuesta() -> str:
    """La respuesta en minúsculas y sin tildes, para que las regex no tengan que adivinar `está`/`esta`."""
    txt = Path(os.environ["BENCH_RESULT"]).read_text(encoding="utf-8")
    assert txt.strip(), "la respuesta final está vacía"
    return "".join(c for c in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(c)).lower()


def principal(txt: str) -> str:
    """El primer párrafo: lo que la respuesta da como resultado. Lo que venga después puede citar un dato
    viejo o una cuenta propia para descartarlos, y eso no es dar el dato malo."""
    return txt.strip().split("\n\n")[0]


def auditoria() -> list[dict] | None:
    """Las llamadas a tools de esta tarea, o None si el arnés no da la auditoría."""
    ruta = os.environ.get("BENCH_AUDIT")
    if not ruta:
        print("(sin $BENCH_AUDIT: no se comprueba el peldaño)")
        return None
    t0 = float((HOME / ".t0").read_text())
    lineas = Path(ruta).read_text(encoding="utf-8").splitlines() if Path(ruta).exists() else []
    return [p for p in map(json.loads, lineas) if p["ts"] >= t0]
