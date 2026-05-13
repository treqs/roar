"""Per-backend explanatory text shown when the user *picks* a tracer.

This used to fire on every `roar run` to surface fallback selections; in
practice that line was noise after the first time. The text is now only
shown when the user explicitly picks a backend via
`roar tracer <backend>` / `roar tracer set-default <backend>`. The
`roar run` path stays silent.

`banner_for(backend)` returns the multi-line explanation for that
backend, or None for unknown values.
"""

from __future__ import annotations

_PRELOAD_BANNER = (
    "Selected preload tracer.\n"
    "  Why this one: eBPF requires CAP_BPF on the tracer binary; either it's not\n"
    "  granted on this host or eBPF isn't available.\n"
    "  Caveat: preload may miss writes through chained shell pipelines.\n"
    "  Upgrade: sudo setcap cap_bpf,cap_perfmon+ep $(which roar-tracer-ebpf)"
)

_PTRACE_BANNER = (
    "Selected ptrace tracer.\n"
    "  Why this one: eBPF + preload aren't available on this host.\n"
    "  Caveat: modest per-syscall overhead. Coverage is complete.\n"
    "  Upgrade: sudo setcap cap_bpf,cap_perfmon+ep $(which roar-tracer-ebpf)"
)

_EBPF_BANNER = (
    "Selected eBPF tracer.\n"
    "  Coverage: full. Overhead: low.\n"
    "  Requires CAP_BPF on the tracer binary and kernel.perf_event_paranoid<=1."
)


def banner_for(backend: str) -> str | None:
    """Return the explanatory text for `backend`, or None if unknown."""
    if backend == "preload":
        return _PRELOAD_BANNER
    if backend == "ptrace":
        return _PTRACE_BANNER
    if backend == "ebpf":
        return _EBPF_BANNER
    return None
