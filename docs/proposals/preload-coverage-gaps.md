# Preload tracer: shell-redirect coverage gaps

## Status

Open. Deferred from the bash-race investigation that produced PR #82 (ptrace
dup3) and PR #83 (eBPF deregister race). The preload tracer's coverage gap
for shell redirects is real but expensive to close and partial at best.

**Severity bump**: The eval team's P0-5 finding is the same gap with worse
framing — multi-stage shell pipelines (`tr | sort | head | awk > out`,
plus chained `*.sh` scripts) produce empty output sets in `roar dag` /
`roar lineage` when the preload backend is the auto-selected fallback.
This breaks the headline value-prop ("if it ran, roar saw it") on the
default backend for every dev machine without `CAP_BPF`. See "Empirical
matrix" below. There are now two reasonable paths and one preferred one,
all listed in "What it would take to close the gap."

## Reproducer

```bash
# 50 trials each
python3 scripts/test_tracers_sandbox.py --trials 50 --backends preload \
  --command "bash -c 'echo hi > test.txt'"
# preload: 0/50 captured (0%)

python3 scripts/test_tracers_sandbox.py --trials 50 --backends preload \
  --command "python3 -c \"open('test.txt','w').write('hi')\""
# preload: 50/50 captured (100%)
```

Same workload, two backends; preload is correct for Python and zero for the
bash redirect.

## Root causes

Two compounding gaps:

### 1. C interposers are linker-stripped from the .so

`rust/tracers/preload/src/interpose.c` defines C-level interposers for
`open()`, `open64()`, `openat()`, `openat64()`, `creat()`. They live in the
static archive `libroar_preload_interpose.a` produced by `build.rs`, but
`nm -D libroar_tracer_preload.so` shows none of them in the export table —
only the Rust `#[no_mangle]` hooks (`read`, `write`, `fwrite`, `fopen`,
`fdopen`, `freopen`, `unlink`, `unlinkat`, `truncate`, `ftruncate`, `mmap`,
`rename`, `renameat`, `link`, `linkat`, `sendfile`, `copy_file_range`,
`readv`, `writev`, `pread*`, `pwrite*`).

The Rust linker dead-strips functions that nothing in the Rust crate
references by name. The current keep-alive trick is one symbol:

```rust
// lib.rs:30-38
unsafe extern "C" {
    fn roar_preload_interpose_keep() -> c_int;
}
static _ROAR_PRELOAD_INTERPOSE_KEEP: unsafe extern "C" fn() -> c_int =
    roar_preload_interpose_keep;
```

That pulls in the .o for `roar_preload_interpose_keep`, but `-ffunction-sections`
+ `-Wl,--gc-sections` still drop the per-function sections inside that .o
that aren't referenced.

### 2. Bash never calls `write()` via PLT for shell redirects

Bash imports `write@GLIBC_2.17`, `fputs@GLIBC_2.17`, `__fprintf_chk@GLIBC_2.17`,
`__printf_chk@GLIBC_2.17`. `LD_DEBUG=bindings` confirms bash binds `write`
to libroar's hook. But our debug instrumentation (writing to a marker file
via direct syscalls inside the write hook to avoid recursion) shows the
hook is never called from the bash child for `bash -c 'echo > x'` — only
from the parent (`roar-tracer-preload` writing the report).

Bash's `echo` builtin with redirect routes through `fputs` / fortified
`__fprintf_chk` family. Inside glibc those resolve to internal symbols
(`_IO_new_file_xsputn`, `_IO_default_xsputn`) that ultimately call the
libc-private `__write` alias — a hidden visibility symbol that goes
straight to the syscall, **not through bash's PLT**, so the LD_PRELOAD
override on bash's `write` PLT slot is never reached.

Confirmed empirically:

| workload                                           | preload capture |
| -------------------------------------------------- | --------------- |
| C: `open + write(fd, ...)`                         | 100%            |
| C: `open + dup3(fd,1) + write(1, ...)`             | 100%            |
| C: `open + dup3(fd,1) + fputs + fflush`            | 100% (`fwrite` hook covers it) |
| `bash -c 'echo hi > x'`                            | 0%              |
| `bash -c 'sleep 0.2; echo hi > x'`                 | 0%              |

The C `fputs` case captures because glibc routes user `fputs` calls
through `fwrite` (which libroar exports). Bash's path appears to skip
`fwrite` entirely.

## Empirical matrix — preload vs other backends on shell pipelines

Reproduction harness: `scripts/test_tracers_sandbox.py` and the ad-hoc
`p05_test.py` used during the P0-5 investigation. Backends tested at the
state of PR #82 + #83 applied:

| workload                                                     | ptrace (with PR #82) | preload | eBPF (with PR #83, sudo) |
| ------------------------------------------------------------ | -------------------- | ------- | ------------------------ |
| `bash -c 'echo > out'` (single redirect)                     | WRITTEN              | MISSED  | WRITTEN                  |
| `echo a b c \| tr ' ' '\n' \| sort \| head -n 2 > out`       | WRITTEN              | WRITTEN | WRITTEN                  |
| `awk '{print toupper($0)}' > out; cat out`                   | WRITTEN              | MISSED  | WRITTEN                  |
| 3-stage script (echo>corpus, wc\|awk>summary, awk>score)     | all WRITTEN          | corpus/summary READ-only, **score MISSED** | all WRITTEN |

The pipe case (`tr | sort | head > out`) captures under preload because
`head` writes via `write(2)` directly — that hook is exported and fires.
The redirect cases miss because `bash`'s redirect emits via `fputs` /
`__fprintf_chk` which bypass the PLT. `awk` is the bigger problem: it
uses `printf`-family for its own output, so even `awk > out` (no shell
redirect) misses.

This is exactly the P0-5 reproduction: `score.txt` ends up classified
as Read (next stage's `awk` reads it) but never as Written, and
`roar lineage` returns empty.

## What it would take to close the gap

### Option A — keep adding hooks

Add Rust `#[no_mangle] pub extern "C" fn` hooks for, at minimum:
`fputs`, `puts`, `fputc`, `putchar`, `printf`, `fprintf`, `vprintf`,
`vfprintf`, plus the fortified variants `__fprintf_chk`, `__printf_chk`,
`__vfprintf_chk`, `__snprintf_chk`. Each repeats the existing `fwrite`
pattern: set `IN_HOOK=true`, call `real_*` via `dlsym(RTLD_NEXT)`, emit
`fd_path(fileno(stream))` event after.

**Risks**
- Varargs: clean implementations route through the `v*` variants and let
  user-side `printf` fall through. Not all libc versions guarantee that.
- Each glibc internal-routing decision is a moving target; new fortified
  paths added in glibc updates would re-open the gap.
- Performance: `printf`/`fputs` are hot paths; every call now adds a
  dlsym-cached function pointer dereference + a thread-local check.
  Probably fine for shell scripts; may show up as overhead in
  log-heavy workloads.
- Per-stream `fileno()` lookup: usually O(1), occasionally hits
  `_IO_FILE` internal locking under contention.

**Estimate**: ~30 lines per hook × ~12 hooks = ~400 lines of repetitive
boilerplate, plus tests. Each hook is small individually but each is one
more thing the next glibc-internals shift can break.

### Option B — fix the linker-strip and add a few hooks

`__attribute__((used))` on each interposer function in `interpose.c`
restores `open`/`openat`/etc. as exports. Combined with adding `fputs`
and `printf` Rust hooks, this would catch most shell paths at the cost of
a much larger event volume per program (every `open` now fires an event,
including the 50-500 startup opens for `.pyc` / locale / etc.). The
existing 2ms IPC poll loop in `roar-tracer-preload`'s parent may struggle
under that volume; would need to also resize the SO_RCVBUF or tighten
the loop.

### Option C — abandon retrofitting LD_PRELOAD

The robust fix is to interpose at the **syscall** layer rather than the
libc layer. Two paths:

1. **seccomp-bpf user notify**: register a seccomp filter on a small set
   of syscalls (`openat`, `write`, `dup3`, etc.) with `SECCOMP_RET_USER_NOTIF`.
   The kernel suspends the tracee on each filtered syscall and the tracer
   reads/writes via `/proc/<pid>/mem` and unblocks. Pros: no symbol-level
   guessing, catches everything regardless of how libc routes it. Cons:
   significant per-syscall latency, requires `CAP_SYS_ADMIN` for the
   filter installer.
2. **fork+exec with ptrace**: this is just the ptrace tracer.

Conclusion: if shell coverage matters, the answer is "use the ptrace
tracer," not "make preload smarter."

### Option D — document + warn (cheap, partial)

Make the limitation visible at preflight time and in the docs:

- `roar tracer status` warns when preload is the chosen backend AND
  argv[0] resolves to a shell (`bash`, `sh`, `zsh`, `dash`, `ash`, `ksh`,
  `mksh`, `fish`).
- `roar run` prints a one-line note when invoking a shell under preload:
  `note: preload backend may miss shell redirects (echo > file). Use --tracer ptrace for full coverage.`
- README adds a row to the preload limitations: shell builtins that emit
  output via `fputs`/`printf`-family go through libc-internal write paths
  that LD_PRELOAD cannot intercept.

Cost: ~50 lines of warning/doc, no architectural change, no event-volume
risk. Honest about the architectural ceiling. **But** users still hit the
silent gap by default; the warning only triggers for shells they invoke
*directly*. If the user runs `roar run python my_script.py` and that
script in turn calls a shell pipeline, the warning never fires.

### Option E (recommended in light of P0-5) — change the auto-fallback order

Linux `--tracer auto` today is documented as: "prefer eBPF, then preload,
then ptrace." (`README.md`.) On the majority of dev hosts without
`CAP_BPF`, this picks **preload**, which is exactly the silent-gap
backend per the table above. With PR #82 landed, the ptrace tracer
captures all the same workloads correctly (it follows
fork/clone/exec via `PTRACE_O_TRACE*` and now handles `dup3`). Under
ptrace the same 3-stage script captures every read/write. So:

- New auto order on Linux: **eBPF → ptrace → preload**.
- preload remains the macOS default (no Linux ptrace there) and the
  documented "I want low overhead and only have simple workloads"
  fallback.
- Surface the chosen backend at first-run (already partially done by
  `roar tracer status`); add a one-liner in `roar run` output when
  ptrace is selected for the first time per session ("auto-selected
  ptrace backend; this gives full shell-pipeline coverage at modest
  syscall overhead").

Cost: a few lines in `roar/execution/runtime/tracer_backends.py` to
flip the preference order. Plus a doc/README update.

This makes the headline P0-5 issue go away without touching glibc
internals: instead of trying to make preload track everything, we
just pick the backend that already does.

The trade-off: ptrace adds per-syscall cost (a stop/cont pair on every
tracked syscall) which is measurably slower than preload. Real ML
workloads usually don't care (the tracer overhead is dwarfed by the
training step), but log-heavy or syscall-heavy workloads see it. The
ordering above gives users a way to opt in to the cheaper backend
explicitly (`--tracer preload`) when they want speed and know what
their workload looks like.

## What we actually shipped and what's outstanding

Shipped (in #82 / #83):

- ptrace handles `dup3` / `dup2`, taking bash redirect coverage from 0% to
  100% on the ptrace backend.
- eBPF daemon no longer races `pid_to_run` cleanup against in-flight ring-buffer
  events, taking eBPF capture from ~22% to ~100% on short-lived writes.

Open (this doc):

- Preload backend remains 0% on shell-redirect workloads. Recommended next
  step in priority order:
  1. **Option E** (small auto-tracer reorder + doc). Makes P0-5 go away
     for the default install without touching glibc internals. Pairs
     well with Option D as a belt-and-suspenders fallback warning.
  2. Option D (warn-and-document) on its own if Option E is rejected
     for performance reasons.
  3. Option A (selective hook expansion) only if a real workload hits
     the gap *under ptrace* — i.e. when ptrace itself isn't a viable
     escape hatch.

## References

- `rust/tracers/preload/src/lib.rs` — Rust hooks
- `rust/tracers/preload/src/interpose.c` — C interposers (linker-stripped)
- `rust/tracers/preload/src/main.rs` — launcher / IPC daemon
- `scripts/test_tracers_sandbox.py` — race-test harness
- Investigation: see commit message for #82 + #83 for the reproducer
  numbers.
