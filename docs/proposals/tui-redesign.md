# roar TUI redesign

## Goals

`roar tui` v1 (current `cg/roar-tui`) ships a working browser: DAG tree on
the left, tabbed job/artifact detail on the right, log screen, search, and
a tmux launcher. This doc captures the next iteration: a more dag-centric
layout, cross-session navigation, write-capable editors, and a diff viewer.

## Layout

**Boot**: full-width interactive `roar dag` tree on the active session.
No bottom pane until you drill in.

**On `Enter` over a step**: a horizontal split appears — DAG on top,
**job detail** beneath. `Esc` collapses the detail back. Selecting an
artifact reveals the same split with **artifact detail** instead.

Rationale: DAG trees grow tall, not wide. A horizontal split keeps long
command lines and indented trees readable; the eye moves top→bottom
naturally as you drill in.

```
┌─────────────────────────────────────────┐
│ session @abc12345 · 2026-05-06 14:21    │  status row
├─────────────────────────────────────────┤
│ @1 [active] python preprocess.py …      │
│   ⬇ data.csv                            │  full-height DAG when no
│ @2 [stale]  python train.py …           │  detail pane open
│   ⬇ model.pt                            │
│ @3 [active] python evaluate.py …        │
├─────────────────────────────────────────┤
│ Job @2 · python train.py …              │  reveals on Enter,
│ status: stale  duration: 2m11s          │  collapses on Esc
│ inputs: data.csv                        │
│ outputs: model.pt                       │
└─────────────────────────────────────────┘
 [ q ] [ ? ] [ : ] [ / ] [ ! ]    footer
```

## Refresh

Auto-refresh every **5 s**. Tree state (cursor, expansion) is preserved
across reloads. If a detail pane is open on a still-existing entity, it
refreshes too. Cheap because reads route through the same query functions
the CLI uses.

Reason for auto vs. manual: another terminal may be running `roar run`;
the TUI should just track without the user having to hit `r`.

## Navigation

| Scope      | Key            | Action                                                      |
| ---------- | -------------- | ----------------------------------------------------------- |
| Global     | `q`            | quit / pop screen                                           |
| Global     | `?`            | context-aware help                                          |
| Global     | `:`            | command palette (`:diff`, `:labels`, `:config`, `:show`)    |
| Global     | `/`            | search across active session                                |
| Global     | `!`            | tmux launcher                                               |
| Global     | `[` `]`        | prev / next session                                         |
| Global     | `s`            | session picker (modal)                                      |
| Global     | `Tab` `Esc`    | switch pane / back-out                                      |
| Tree       | `↑↓ jk`        | move                                                        |
| Tree       | `←→`           | collapse / expand                                           |
| Tree       | `Enter`        | drill into highlighted node                                 |
| Tree       | `g G`          | top / bottom                                                |
| Job detail | `o i O e c l g p` | jump to section                                          |

`Ctrl`-modified keys are reserved for terminal pass-through and to avoid
clashes with text inputs (search, label editor, command palette).

## Sessions

`[`/`]` page through historical sessions read-only. The active session is
marked with `ACTIVE` in the status row. Selection persistence: when paging,
try to preserve the same step number / job uid so re-runs are easy to
compare.

`s` opens a modal list of all sessions for the project (date, hash,
command, job count). Enter selects.

This requires `build_dag_visualization` to take a session id rather than
always pulling the active session — small refactor mirroring
`build_show_summary`.

## Job detail

A single tall scrollable form with anchored sections (Overview / Inputs /
Outputs / Env / Command / Labels / Git / Packages / Timing / Tracer / raw
JSON); single-key jumps land on each anchor. Scrolling form > tab strip
because there are too many sections to fit horizontally and skim-reading
beats cycling.

## Editors and viewers (deferred to follow-up branches)

- **Label editor** — operates on the currently selected entity (session,
  job, or artifact). Reuses `roar.application.labels.*` for writes. Modal
  form.
- **Config editor** — project `.roar/config.toml` only in v1. Global
  config stays in the `roar config` CLI.
- **Diff viewer** — diff two jobs (cmd / env / inputs / outputs). Reuses
  `cg/roar-diff` output. Pushed screen, reachable via `:diff @3 @5` or
  selecting two jobs in the tree.
- **Help screen** — context-aware overlay; keys for the focused pane only.

## Architectural rules (carried over from v1)

1. **TUI is a pure view layer**: all reads through `application.query.*`,
   all writes through the same service functions the CLI uses. No new SQL.
2. **Launcher requires tmux**: no log-file fallback.
3. **Detail panes decoupled from host screens**: the same `JobDetail`
   widget should drop into a future diff screen.

## Implementation order

1. ✅ Layout swap + auto-refresh.
2. `[` / `]` navigation. (Multi-session support landed in `build_dag_visualization` and `tui.data.list_sessions` — UI keys still pending.)
3. Session picker (`s`).
4. ✅ Job detail as scrollable form with anchors. (Shipped with a sticky TOC sidebar that doubles as a section indicator + click-jump target.)
5. Command palette (`:`).
6. Label editor.
7. Config editor.
8. Diff viewer.
9. Context-aware help (`?`).

## Tweaks that landed alongside the above

- `q` is now a context-sensitive Back/Quit (closes the detail pane if open,
  exits otherwise). Esc still works as an alternative but pays the
  terminal's ESC-prefix disambiguation delay.
- ESCDELAY pinned to 25 ms inside the TUI so Esc fires snappily on
  terminals that don't already have it tuned.
- Enter on a tree node moves focus to the detail body (so PageDown
  works without a Tab first); Tab toggles focus tree ↔ detail.
- TOC rows show their jump key inline (`▸ s Summary`, `  i Producers`,
  …) so shortcuts are visible without consulting a help screen.
- `i`/`o` are paired across job (Inputs/Outputs) and artifact
  (Producers/Consumers — same flow direction). `s` is Summary in both.
- Detail widgets live in `roar/tui/widgets/detail.py` so the future
  diff viewer can drop them in.
