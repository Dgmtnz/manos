# manos

**Hands for your AI agent on a real Linux desktop.**

manos ("hands" in Spanish) is an MCP server that lets an agent such as Claude Code operate a KDE Plasma 6 desktop on
Wayland: it browses and fills in websites, extracts data, works with spreadsheets and documents, and drives desktop
apps. The model does the thinking. manos supplies the hands and the brakes, and it never sends, pays or types a
secret without you.

```text
you:    Look up tomorrow's forecast for Murcia on aemet.es, then put the LaLiga table in tabla.xlsx
        with a points-per-game column.
agent:  Max 26 °C, min 13 °C, 0 % rain. Heads-up: AEMET has a heavy-rain warning for the Vega del Segura.
        tabla.xlsx saved: 20 teams, column L = points per game. Verified by the server.
```

## Why manos

Most computer-use agents look at screenshots and click on pixels. That is slow, expensive and fragile. manos takes
the most reliable route available for each step, and drops to pixels only as a last resort:

| Rung | How it acts | Used for |
|---|---|---|
| 1 | Application menu actions by name over D-Bus (KXmlGui) | Every KDE app: `file_save`, `create_dir`, `edit_paste`… |
| 2 | The accessibility tree (AT-SPI): roles, names, states | Any accessible app, GTK included |
| 3 | Screenshot plus synthetic keyboard and mouse (libei) | Whatever the first two cannot reach |
| Web | A dedicated Brave (Chromium) over CDP, with its own profile, read as elements rather than pixels | Sites, forms, iframes, shadow DOM, dialogs |
| Office | Headless LibreOffice over UNO, no windows | .ods .xlsx .csv .odt .docx .pdf |

In the benchmark, only **6.3 %** of acting steps needed pixels.

## Measured, not claimed

- **35-task core benchmark, Claude Sonnet: 97 % pass (34/35), 0 false "done", $2.72 for the full run.** The one miss
  was the agent stopping to ask which orders to notify, which is a correct stop.
- **Real-world round on a live desktop: 7/8 done, plus 1 correct handoff** (a Cloudflare challenge), $1.20 in total.
  Every answer was checked against the real data.
- **The server checks "done" itself.** `submit_task_complete` re-runs the task's acceptance criteria (files, cells,
  page state, windows) before it reports success. If it can't prove the task is done, it doesn't say so.

## What it can do (30 tools)

- **Web.** Open, navigate, observe the page as a numbered list of actionable elements, click, type, upload, answer
  native dialogs and work inside iframes.
- **Forms in one call.** `form_fill` fills a whole form server-side: dates, input masks, custom selects, multi-step
  wizards and shadow DOM. It then re-reads every field and checks validity. Sending is a separate step that needs a
  one-time token.
- **Bulk extraction.** Paginated tables or JSON APIs go straight to `.csv`/`.ndjson` without passing through the
  model's context. Extraction is resumable and backs off on `429 Retry-After`.
- **Downloads.** Saves a file or image by URL, or by pointing at a thumbnail or link on the page. It never overwrites
  an existing file.
- **Office without windows.** Read, write (with formulas) and convert spreadsheets and documents.
- **Desktop apps.** Launch apps, use their menus by name, read and act on any accessible UI, and place windows.
- **WebMCP.** If a page declares its own tools, the agent calls them directly.
- **Gets cheaper with use.**
  - A task the server has certified can be replayed step by step without re-thinking it.
  - After every certified task, manos records what worked in that app, and the next task reads it first.
- **Long tasks.** Checkpoints, resume by name on another day, and `ask_human` for decisions only you can make.

## Safety is the product

| Brake | What it does |
|---|---|
| **Emergency stop** `Meta+Alt+Esc` | Freezes the browser and every action in ~10 ms through a cgroup freeze. It bypasses the MCP server entirely and can be undone. |
| **Take the wheel** `Meta+Shift+B` | Brings the agent's browser to you; the agent stays hands-off until you say "go on". |
| **Handoff** | Passwords, PINs, one-time codes, cards, captchas, anti-bot checks and logins go to you, never the model. |
| **Two-step sends** | Pay, send, delete and confirm need your approval, and replay never repeats them. |
| **No secrets on disk** | Your form profile (`~/.config/manos/profile.toml`) refuses to load if it looks like it contains a secret. |
| **Blind spots by design** | Password managers and other sensitive windows are never read. Hidden folders under `~` are never uploaded or opened. |
| **Cookie banners** | Only ever rejected, never accepted on your behalf. |
| **Audit log** | One line per action (tool, rung, ok/error) with no values, URLs or page text. |

The agent's browser lives off-screen so it never covers your work. Ask the agent to work in watch mode and it stays
visible, with **shared control**: you can solve a captcha right in the window while the agent keeps going.

## Quick start

Requirements:
- KDE Plasma 6 on Wayland, with a systemd user session
- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- LibreOffice with its Python UNO bridge, for the office tools
- [Brave](https://brave.com) at `/opt/brave-bin/brave` (Arch: `brave-bin`); the agent runs it with its own profile, separate from yours
- An MCP client; it is built and tuned for [Claude Code](https://docs.claude.com/en/docs/claude-code)

```bash
git clone https://github.com/Dgmtnz/manos && cd manos
uv sync
uv run --project . server.py --diag       # checks the machine: PASS / FAIL / SKIP per line

claude mcp add -s user manos -- uv run --project "$PWD" "$PWD/server.py"

panic/manos-panic --install               # then assign Meta+Alt+Esc in System Settings > Shortcuts
panic/manos-summon --install              # then assign Meta+Shift+B
```

Then just ask:

```bash
cd ~/Documents/somewhere && claude --model sonnet
> Read ~/Downloads/expenses.xlsx, total it by category and save the summary to summary.xlsx.
> In Dolphin, create Downloads/pdf-november and move this month's PDFs into it.
> Search Google Images for cats and download the 5 cutest to my Desktop. Let me watch.
```

The full user guide (in Spanish) is in [`docs/tutorial.md`](docs/tutorial.md).

## Testing

```bash
uv run --project . server.py --selftest                       # unit, browser and nested-KWin tests: "todo OK"
uv run --project . bench/run.py --core --model sonnet         # the 35-task benchmark
```

Desktop tests run in a **nested, isolated KWin session**, so they never touch your live desktop. The benchmark scores
each task by an independent verifier, not by the agent's own claim.

## Project layout

| Path | What |
|---|---|
| `server.py` | The MCP server: tools, instructions, the completion gate |
| `browser/` | Chromium session, page observation, actions, forms, extraction, sanitising, consent, WebMCP |
| `desktop/` | AT-SPI, app menus, input over libei, screen capture, Spanish keymap |
| `kwin.py` | Window placement and the KWin scripts behind parking and the focus guard |
| `office.py`, `office_worker.py` | Headless LibreOffice |
| `verify.py`, `replay.py`, `memory.py` | Completion checks, certified replay, per-app memory |
| `panic/` | Emergency stop and takeover, independent of the server |
| `skills/` | Method and form-filling guides the agent loads on demand |
| `bench/`, `tests/` | Benchmark tasks with verifiers, and the self-test |

## Status

manos is a working, tested personal project, built and measured on one machine: CachyOS with Plasma 6.8 on Wayland.
Its code comments, prompts and guide are in Spanish, and the prompts address the user as Diego. Change that in
`server.py` if you adopt it. The browser path is pinned to the installed Brave binary (`CHROME` in `browser/session.py`).

## License

[MIT](LICENSE)
