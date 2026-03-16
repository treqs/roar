from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from roar.integrations.glaas import GlaasFragmentStreamer

FragmentLocalMerge = Callable[[list[dict[str, Any]], str, str | None], None]
FragmentLocalFallback = Callable[[], None]
FragmentEmitResult = Literal["streamed", "merged", "fallback", "skipped"]


def emit_fragment_dicts(
    fragments: Sequence[dict[str, Any]],
    *,
    env: Mapping[str, str] | None = None,
    local_merge: FragmentLocalMerge | None = None,
    local_fallback: FragmentLocalFallback | None = None,
) -> FragmentEmitResult:
    normalized_fragments = [fragment for fragment in fragments if isinstance(fragment, dict)]
    if not normalized_fragments:
        return "skipped"

    resolved_env = os.environ if env is None else env
    session_id = str(resolved_env.get("ROAR_SESSION_ID", "")).strip()
    token = str(resolved_env.get("ROAR_FRAGMENT_TOKEN", "")).strip()
    glaas_url = str(resolved_env.get("GLAAS_URL") or "").strip()

    if session_id and token and glaas_url:
        try:
            streamer = GlaasFragmentStreamer(
                session_id=session_id,
                token=token,
                glaas_url=glaas_url,
            )
            for fragment in normalized_fragments:
                streamer.append_fragment(fragment)
            streamer.close()
            return "streamed"
        except Exception:
            pass

    project_dir = str(resolved_env.get("ROAR_PROJECT_DIR", "")).strip()
    if local_merge is not None and project_dir:
        driver_job_uid = str(resolved_env.get("ROAR_JOB_ID", "")).strip() or None
        local_merge(normalized_fragments, project_dir, driver_job_uid)
        return "merged"

    if local_fallback is not None:
        local_fallback()
        return "fallback"

    return "skipped"
