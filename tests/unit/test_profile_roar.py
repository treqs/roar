from __future__ import annotations

from scripts.profile_roar import _parse_importtime, _summary_stats


def test_parse_importtime_returns_top_cumulative_modules() -> None:
    stderr = "\n".join(
        [
            "import time:       100 |        100 | _io",
            "import time:       200 |        500 | pathlib",
            "import time:        50 |        900 | roar.execution.runtime.inject.sitecustomize",
            "not an importtime line",
        ]
    )

    results = _parse_importtime(stderr, limit=2)

    assert [item.module for item in results] == [
        "roar.execution.runtime.inject.sitecustomize",
        "pathlib",
    ]
    assert results[0].cumulative_ms == 0.9
    assert results[1].self_ms == 0.2


def test_summary_stats_handles_single_sample() -> None:
    summary = _summary_stats([12.5])

    assert summary["mean_ms"] == 12.5
    assert summary["median_ms"] == 12.5
    assert summary["min_ms"] == 12.5
    assert summary["max_ms"] == 12.5
    assert summary["stdev_ms"] == 0.0
