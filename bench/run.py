"""El banco de pruebas (PLAN §10.2): cada tarea con `claude -p` contra manos, puntuación binaria.

    uv run --project . bench/run.py [--tasks id,id | --core] [-k 3] [--model sonnet] [--dry-run] [--warm]

Por ejecución: `$BENCH_HOME` nuevo bajo bench/run/, setup, agente, verify y teardown. Pasa si verify sale con 0
**y** el gate aceptó un submit_task_complete en esa ejecución (`pass`); solo verify es `pass_verify`. Las tareas
con `"gate": false` (inviables, o con un final honesto que no se puede certificar) pasan solo con verify. Una
línea JSON por ejecución en bench/results/<fecha>.jsonl y, al final, el resumen y la fila de PLAN §12.

- El agente no carga la configuración personal de Diego: `--setting-sources ''` (ni CLAUDE.md global, ni
  hooks, ni plugins), `--strict-mcp-config` (solo manos) y la auto-memoria apagada por entorno.
  `--restricted` quita Bash y deja leer y escribir solo en los directorios de trabajo.
- Las tareas `desktop` (o con `"nest": true`) corren en la KWin anidada de tests/nest.py: claude y el
  servidor heredan el entorno anidado y MANOS_KWIN_BUS/MANOS_KWIN_LOG; HOME sigue siendo el real.
- `XDG_DATA_HOME` es propio de cada ejecución (lo comparten setup, servidor y verify): de ahí salen la
  auditoría (`$BENCH_AUDIT`, peldaños) y las tareas de submit_task_complete. El navegador usa un perfil
  desechable (`MANOS_BROWSER_PROFILE`); el fichero de parada sigue colgando de HOME.
- En la anidada arranca at-spi2-registryd (nadie lo activa allí). `$BENCH_RESULT` lleva la respuesta final.
- El presupuesto `budget_s` se cumple matando el grupo de procesos de claude por su pid.
- `--warm` mide la caché de repetición (replay.py) y la memoria de apps (memory.py): por tarea, k=0 en frío; cada
  ejecución siguiente empieza con un `XDG_DATA_HOME` nuevo que solo trae `manos/replay/` y `manos/memory/` de la
  k=0 (ni tareas ni auditoría). El resumen
  compara frío y caliente: pass@1, y la mediana de coste, turnos y llamadas a tools.
"""

import argparse
import datetime
import io
import json
import os
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import contextlib
from collections import Counter
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
# las skill:// de manos son resources: sin las dos últimas el modelo no puede leerlas
TOOLS = "Read,Write,Edit,Glob,Grep,ListMcpResourcesTool,ReadMcpResourceTool"
# en vivo el agente no toca el escritorio de Diego: solo navegador, formularios y tareas
DESK = ",".join(f"mcp__manos__{t}" for t in ("desktop_state", "app_actions", "app_invoke", "window_set",
                                             "app_launch", "screen_capture", "ui_snapshot", "ui_act"))
STEP_S = 180  # setup, verify y teardown
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]  # nest.py y desktop.atspi, solo para las tareas anidadas


def claude_cmd(instruction: str, model: str, mcp: Path, home: Path, nest: bool) -> list[str]:
    # el prompt va pegado a -p: tras una opción variádica (--add-dir, --allowedTools) se la tragaría
    return ["claude", "-p", instruction, "--model", model, "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp), "--strict-mcp-config", "--setting-sources", "", "--restricted",
            "--tools", TOOLS, "--allowedTools", f"mcp__manos,{TOOLS}", *([] if nest else ["--disallowedTools", DESK]),
            "--permission-mode", "dontAsk", "--no-session-persistence", "--add-dir", str(home)]


def task_env(task: dict, home: Path, run: Path, nest) -> dict:
    if nest:  # el entorno anidado entero (lista blanca de nest.py), no el vivo con tres claves cambiadas
        base = {**nest.env, "MANOS_KWIN_BUS": nest.address, "MANOS_KWIN_LOG": str(nest.log)}
    else:  # sin las marcas de la sesión de claude que lance esto
        base = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env = base | {"BENCH_HOME": str(home), "BENCH_RESULT": str(run / "result.txt"),
                  "XDG_DATA_HOME": str(run / "data"), "BENCH_AUDIT": str(run / "data/manos/audit.jsonl"),
                  "MANOS_BROWSER_PROFILE": str(run / "browser-profile"), "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    return env | {k: Template(v).safe_substitute(env) for k, v in task.get("env", {}).items()}


def step(cmd: str, env: dict, log) -> int:
    try:
        return subprocess.run(cmd, shell=True, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=log, timeout=STEP_S).returncode
    except subprocess.TimeoutExpired:
        return -1


def agent(cmd: list[str], env: dict, home: Path, run: Path, budget: float) -> bool:
    """Corre claude hasta que acaba o vence el presupuesto. True si hubo que matarlo."""
    with open(run / "stream.jsonl", "wb") as out, open(run / "claude.err", "wb") as err:
        p = subprocess.Popen(cmd, cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             start_new_session=True)
        try:
            p.wait(timeout=budget)
            return False
        except subprocess.TimeoutExpired:
            return True
        finally:  # vencido o Ctrl-C (claude va en su propia sesión y no lo recibe): el grupo, por su pid
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if p.poll() is not None:
                    break
                with contextlib.suppress(ProcessLookupError, subprocess.TimeoutExpired):
                    os.killpg(p.pid, sig)
                    p.wait(timeout=10)


def _result_json(block: dict) -> dict:
    content = block.get("content")
    texts = [content] if isinstance(content, str) else [c.get("text", "") for c in content or [] if isinstance(c, dict)]
    for t in texts:
        try:
            d = json.loads(t)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict):
            return d
    return {}


def digest(stream: Path) -> dict:
    """Lo que dice el stream-json de claude: tools, turnos, tokens, coste, Stuck/hint, gate y errores."""
    calls, errors, names = Counter(), Counter(), {}
    hints, gate, out = 0, [], {"final": "", "model_id": None, "turns": 0, "tokens": {}, "cost_usd": None}
    for line in stream.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            out["model_id"] = e.get("model")
        elif kind == "assistant":
            for b in e["message"].get("content", []):
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"].removeprefix("mcp__manos__")
                    calls[names[b["id"]]] += 1
        elif kind == "user" and isinstance(e["message"].get("content"), list):
            for b in e["message"]["content"]:
                if b.get("type") != "tool_result":
                    continue
                res = _result_json(b)
                err = res.get("error") if isinstance(res.get("error"), dict) else None
                kind_err = err and err.get("type") or ("ToolError" if b.get("is_error") else None)
                if kind_err:
                    errors[kind_err] += 1
                hints += "hint" in res
                if names.get(b.get("tool_use_id")) == "submit_task_complete":
                    gate.append(not b.get("is_error") and res.get("ok", True) is True)
        elif kind == "result":
            u = e.get("usage") or {}
            out |= {"final": e.get("result") or "", "turns": e.get("num_turns", 0), "cost_usd": e.get("total_cost_usd"),
                    "tokens": {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                               "cache_read": u.get("cache_read_input_tokens", 0),
                               "cache_write": u.get("cache_creation_input_tokens", 0)}}
    acc = sum(gate)
    out |= {"calls": dict(calls), "errors": dict(errors), "stuck": errors["Stuck"], "hints": hints,
            "gate": {"accepted": acc, "rejected": len(gate) - acc,
                     "conversions": int(acc > 0 and False in gate[:gate.index(True)])}}
    return out


def passed(task: dict, verify_ok: bool, gate: dict) -> bool:
    """El resultado puntuado: el verificador y, salvo `"gate": false`, un cierre aceptado por el gate."""
    return verify_ok and (not task.get("gate", True) or gate["accepted"] > 0)


def false_done(r: dict) -> bool:
    """El gate aceptó un cierre y el verificador suspendió: trabajo mal hecho certificado por el propio modelo."""
    return r["gate"]["accepted"] > 0 and not r["pass_verify"]


def _audit(run: Path) -> list[dict]:
    audit = run / "data/manos/audit.jsonl"
    return [json.loads(l) for l in audit.read_text().splitlines()] if audit.exists() else []


def rungs(run: Path) -> dict:
    """Histograma de peldaños de los pasos que actúan (PLAN §10.2), de la auditoría: escritorio y web. Las
    lecturas (`acting: false`) no cuentan; las llamadas, todas, van en `calls`."""
    return dict(Counter(l["rung"] for l in _audit(run) if l["acting"]))


def revisions(run: Path) -> int:
    """Criterios de aceptación corregidos con `task_checkpoint(revise=…)`: muchos delatan un gate rebajado."""
    return sum(l["tool"] == "task_checkpoint" and l.get("action") == "revise" for l in _audit(run))


def a11y(n) -> None:
    """registryd en la anidada, como tests/test_atspi_nest.py: allí no hay systemd que lo active.
    El primer healthy() activa el lanzador del bus a11y, y registryd necesita ese bus para registrarse."""
    from desktop import atspi
    atspi.healthy()
    n.launch(["/usr/lib/at-spi2-registryd"])
    deadline = time.monotonic() + 10
    while not atspi.healthy():
        if time.monotonic() > deadline:
            raise RuntimeError("registryd no contesta en la anidada")
        time.sleep(0.1)


def seed_replays(src: Path, run: Path) -> None:
    """La ejecución `run` empieza con las repeticiones y la memoria de apps de `src`, y nada más de su XDG_DATA_HOME."""
    (run / "data/manos").mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in ("replay", "memory"):
        if (src / "data/manos" / d).is_dir():
            shutil.copytree(src / "data/manos" / d, run / "data/manos" / d)


def run_one(task: dict, i: int, model: str, dry: bool, seed: Path | None = None) -> dict | None:
    nest = task.get("nest") or "desktop" in task.get("tags", [])
    (BENCH / "run").mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(dir=BENCH / "run", prefix=f"{task['id']}-k{i}-"))
    home = run / "home"
    home.mkdir()
    (run / "data").mkdir()
    if seed:
        seed_replays(seed, run)
    mcp = run / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"manos": {"command": "uv", "args": [
        "run", "--project", str(ROOT), str(ROOT / "server.py")]}}}))
    if nest:
        from nest import nested
    with (nested(1280, 800) if nest else contextlib.nullcontext()) as n, open(run / "steps.log", "w") as log:
        if n:
            a11y(n)
        env = task_env(task, home, run, n)
        cmd = claude_cmd(task["instruction"], model, mcp, home, bool(n))
        t0 = time.monotonic()
        rc_setup = step(task["setup"], env, log)
        if dry:
            live = os.environ if not n else n.live
            print(f"[{task['id']}] setup rc={rc_setup} · nest={bool(n)} · cwd={home}\n{shlex.join(cmd)}")
            print("env +/~:", json.dumps({k: v for k, v in env.items() if live.get(k) != v}, indent=1, ensure_ascii=False))
            print("env -:", sorted(k for k in live if k not in env))
            print(f"[{task['id']}] teardown rc={step(task['teardown'], env, log)}")
        else:
            killed = rc_setup == 0 and agent(cmd, env, home, run, task.get("budget_s", 300))
            d = digest(run / "stream.jsonl") if rc_setup == 0 else digest(Path(os.devnull))
            (run / "result.txt").write_text(d["final"])
            rc_verify = step(task["verify"], env, log) if rc_setup == 0 else None
            wall = round(time.monotonic() - t0, 1)
            rc_teardown = step(task["teardown"], env, log)
    shutil.rmtree(run / "browser-profile", ignore_errors=True)  # ~decenas de MB por ejecución
    if dry:
        shutil.rmtree(run)
        return None
    row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "task": task["id"], "k": i, "model": model,
           "model_id": d["model_id"], "pass": passed(task, rc_verify == 0, d["gate"]), "pass_verify": rc_verify == 0,
           "wall_s": wall, "timeout": killed,
           "setup_rc": rc_setup, "verify_rc": rc_verify, "teardown_rc": rc_teardown, "nest": bool(nest),
           "calls": d["calls"], "turns": d["turns"], "tokens": d["tokens"], "cost_usd": d["cost_usd"],
           "rungs": rungs(run), "revisions": revisions(run), "stuck": d["stuck"], "hints": d["hints"], "gate": d["gate"],
           "errors": d["errors"], "run_dir": str(run.relative_to(ROOT)), "warm": seed is not None}
    row["false_done"] = false_done(row)
    (BENCH / "results").mkdir(exist_ok=True)
    with open(BENCH / "results" / f"{datetime.date.today()}.jsonl", "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


def summary(rows: list[dict], k: int, model: str) -> None:
    by, bv, bf = {}, {}, Counter()  # con gate, solo verify y falsos «hecho»
    for r in rows:
        by.setdefault(r["task"], []).append(int(r["pass"]))
        bv.setdefault(r["task"], []).append(int(r["pass_verify"]))
        bf[r["task"]] += false_done(r)
    print("\ntarea                     pass@1  pass@k  var   verify@1  falso")
    for t, p in by.items():
        print(f"{t:25s} {statistics.mean(p):6.2f}  {max(p):6d}  {statistics.pvariance(p):.2f}  "
              f"{statistics.mean(bv[t]):6.2f}  {bf[t]:5d}")
    p1 = statistics.mean(statistics.mean(p) for p in by.values())
    pk = statistics.mean(max(p) for p in by.values())
    v1 = statistics.mean(statistics.mean(p) for p in bv.values())
    print(f"{'TOTAL':25s} {p1:6.2f}  {pk:6.2f}        {v1:6.2f}   ({len(rows)} ejecuciones, {len(by)} tareas, k={k})")
    print(f"pass@1 {p1:.0%} con gate · {v1:.0%} solo verificador · falsos «hecho» {bf.total()} (gate sí, verify no)")
    hist = sum((Counter(r["rungs"]) for r in rows), Counter())
    px = hist["3"] / sum(hist.values()) if hist else 0  # píxeles: peldaño 3 sobre los pasos que actúan
    tools = statistics.mean(sum(r["calls"].values()) for r in rows)
    tok_in = statistics.mean(sum(v for n, v in r["tokens"].items() if n != "output") for r in rows)
    tok_out = statistics.mean(r["tokens"].get("output", 0) for r in rows)
    rej = sum(r["gate"]["rejected"] for r in rows)
    conv = sum(r["gate"]["conversions"] for r in rows)
    cost = sum(r["cost_usd"] or 0 for r in rows)
    revs = sum(r.get("revisions", 0) for r in rows)
    print(f"peldaños {dict(hist)} · criterios corregidos {revs} · coste ${cost:.2f}")
    if any(r.get("warm") for r in rows):
        print(warm_summary(rows))
    print(f"\n| — | {datetime.date.today()} | {model} | {p1:.0%} | {pk:.0%}{'' if k == 3 else f' (k={k})'} | {px:.1%} | "
          f"{tools:.1f} | {tok_in / 1000:.0f}k/{tok_out / 1000:.1f}k | {rej} | {conv} |")


def warm_summary(rows: list[dict]) -> str:
    """Frío (k=0) contra caliente (las demás, con la caché de la k=0): por tarea y en total. Coste, turnos y llamadas
    son medianas; `replay` cuenta las ejecuciones calientes que llamaron a task_replay."""
    med = lambda rs, f: statistics.median(f(r) for r in rs) if rs else 0
    cost, turns, calls = lambda r: r["cost_usd"] or 0, lambda r: r["turns"], lambda r: sum(r["calls"].values())
    line = lambda name, c, w: (f"{name:25s} {statistics.mean(r['pass'] for r in c) if c else 0:5.2f} "
                               f"{statistics.mean(r['pass'] for r in w) if w else 0:5.2f}  ${med(c, cost):.3f} "
                               f"${med(w, cost):.3f}  {med(c, turns):5.1f} {med(w, turns):5.1f}  {med(c, calls):5.1f} "
                               f"{med(w, calls):5.1f}  {sum('task_replay' in r['calls'] for r in w)}/{len(w)}")
    out = ["\nfrío/caliente             pass@1      coste mediano     turnos      tools      replay"]
    for t in dict.fromkeys(r["task"] for r in rows):
        out.append(line(t, [r for r in rows if r["task"] == t and not r.get("warm")],
                        [r for r in rows if r["task"] == t and r.get("warm")]))
    cold, warm = [r for r in rows if not r.get("warm")], [r for r in rows if r.get("warm")]
    out.append(line("TOTAL", cold, warm))
    c, w = med(cold, cost), med(warm, cost)
    out.append(f"coste mediano caliente {(w - c) / c:+.0%} respecto a frío" if c else "sin coste en frío")
    return "\n".join(out)


def selftest() -> int:
    """digest y rungs sobre un stream inventado: gate con conversión, Stuck, hint, tokens y peldaños."""
    use = lambda i, n: {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": n}]}}
    res = lambda i, d, err=False: {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": i, "is_error": err, "content": [{"type": "text", "text": json.dumps(d)}]}]}}
    ev = [{"type": "system", "subtype": "init", "model": "m"},
          use("a", "mcp__manos__ui_act"), res("a", {"ok": False, "error": {"type": "Stuck"}, "hint": "x"}),
          use("b", "mcp__manos__submit_task_complete"), res("b", {"ok": False, "error": {"type": "NotDone"}}, True),
          use("c", "mcp__manos__submit_task_complete"), res("c", {"ok": True}),
          use("d", "mcp__manos__form_fill"), res("d", {"ok": True}),
          {"type": "result", "result": "hecho", "num_turns": 4, "total_cost_usd": 0.5,
           "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3}}]
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp)
        (run / "s.jsonl").write_text("\n".join(map(json.dumps, ev)) + "\nbasura\n")
        (run / "data/manos").mkdir(parents=True)
        (run / "data/manos/audit.jsonl").write_text("".join(json.dumps(l) + "\n" for l in [
            {"tool": "ui_act", "rung": "3", "acting": True}, {"tool": "ui_act", "rung": "2", "acting": True},
            {"tool": "screen_capture", "rung": "3", "acting": False}, {"tool": "form_fill", "rung": "web", "acting": True},
            {"tool": "browser_observe", "rung": "web", "acting": False},
            {"tool": "task_checkpoint", "rung": None, "action": "revise", "acting": False, "i": 0},
            {"tool": "task_checkpoint", "rung": None, "action": "revise", "acting": False, "i": 2}]))
        d = digest(run / "s.jsonl")
        assert d["gate"] == {"accepted": 1, "rejected": 1, "conversions": 1}, d["gate"]
        assert (d["stuck"], d["hints"], d["errors"]) == (1, 1, {"Stuck": 1, "NotDone": 1}), d
        assert d["calls"] == {"ui_act": 1, "submit_task_complete": 2, "form_fill": 1}, d["calls"]
        assert (d["final"], d["turns"], d["model_id"], d["tokens"]["cache_read"]) == ("hecho", 4, "m", 3), d
        assert rungs(run) == {"3": 1, "2": 1, "web": 1} and revisions(run) == 2, rungs(run)
        assert revisions(Path(tmp) / "no-hay") == 0
    # puntuación: verify y, salvo `"gate": false`, un cierre aceptado; rechazos sin aceptar no bastan
    no, si = {"accepted": 0, "rejected": 2, "conversions": 0}, {"accepted": 1, "rejected": 1, "conversions": 1}
    assert passed({}, True, si) and not passed({}, True, no) and not passed({}, False, si)
    assert passed({"gate": False}, True, no) and not passed({"gate": False}, False, no)
    assert passed({"gate": True}, True, si) and not passed({"gate": True}, True, no)
    # falso «hecho»: gate aceptado y verify suspendido; sin aceptar, o con verify bien, no
    assert false_done({"gate": si, "pass_verify": False}) and not false_done({"gate": no, "pass_verify": False})
    assert not false_done({"gate": si, "pass_verify": True})
    row = lambda t, p, v, g=no: {"task": t, "pass": p, "pass_verify": v, "rungs": {}, "calls": {"x": 1}, "tokens": {},
                                 "gate": g, "cost_usd": 0, "revisions": 1}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        summary([row("a", True, True), row("a", False, True), row("b", False, True), row("b", False, False, si)], 2, "m")
    assert "pass@1 25% con gate · 75% solo verificador · falsos «hecho» 1 (gate sí, verify no)" in out.getvalue(), \
        out.getvalue()
    assert [l.split()[-1] for l in out.getvalue().splitlines()[2:4]] == ["0", "1"], out.getvalue()
    assert "criterios corregidos 4" in out.getvalue(), out.getvalue()
    # --warm: la ejecución nueva solo hereda replay/; el resumen compara frío y caliente
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "k0", Path(tmp) / "k1"
        (src / "data/manos/replay").mkdir(parents=True)
        (src / "data/manos/tasks").mkdir()
        (src / "data/manos/replay/alta.json").write_text("{}")
        (src / "data/manos/audit.jsonl").write_text("{}")
        (dst / "data").mkdir(parents=True)
        seed_replays(src, dst)
        assert sorted(p.relative_to(dst).as_posix() for p in (dst / "data").rglob("*")) == \
            ["data/manos", "data/manos/replay", "data/manos/replay/alta.json"]
        seed_replays(Path(tmp) / "no-hay", Path(tmp) / "k2")  # k=0 sin caché: arranca vacía
        assert not (Path(tmp) / "k2/data/manos/replay").exists()
        (src / "data/manos/memory").mkdir()  # y la memoria de apps, si la hay
        (src / "data/manos/memory/org.kde.kcalc.md").write_text("x")
        seed_replays(src, Path(tmp) / "k3")
        assert (Path(tmp) / "k3/data/manos/memory/org.kde.kcalc.md").read_text() == "x"
    wr = lambda p, c, n, warm, calls: {"task": "a", "pass": p, "cost_usd": c, "turns": n, "calls": calls, "warm": warm}
    s = warm_summary([wr(True, 0.10, 8, False, {"x": 6}), wr(True, 0.04, 4, True, {"task_replay": 1, "x": 2}),
                      wr(False, 0.06, 6, True, {"x": 4})])
    assert "a                          1.00  0.50  $0.100 $0.050    8.0   5.0    6.0   3.5  1/2" in s, s
    assert "coste mediano caliente -50% respecto a frío" in s, s
    print("bench/run: OK")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description="Banco de pruebas de manos (PLAN §10.2)")
    ap.add_argument("--tasks", help="ids separados por comas")
    ap.add_argument("--core", action="store_true", help="las 15 de bench/core.txt")
    ap.add_argument("-k", type=int, default=1, help="repeticiones por tarea")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--dry-run", action="store_true", help="setup, orden y entorno, teardown; sin modelo")
    ap.add_argument("--warm", action="store_true",
                    help="k=0 en frío; las demás con la caché de repetición y la memoria de la k=0")
    a = ap.parse_args()
    if a.core:
        ids = [l.split("#")[0].strip() for l in (BENCH / "core.txt").read_text().splitlines()]
    elif a.tasks:
        ids = a.tasks.split(",")
    else:
        ids = sorted(p.stem for p in (BENCH / "tasks").glob("*.json") if not p.stem.startswith("_"))
    tasks = [json.loads((BENCH / "tasks" / f"{i}.json").read_text()) for i in ids if i]
    rows = []
    for t in tasks:
        first = None
        for i in range(a.k):
            seed = ROOT / first["run_dir"] if a.warm and first else None
            if r := run_one(t, i, a.model, a.dry_run, seed):
                rows.append(r)
                first = first or r
    if rows:
        summary(rows, a.k, a.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
