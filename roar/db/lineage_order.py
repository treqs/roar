"""Chronological producer selection for content-addressed lineage traversal."""

from typing import Any

ProducerOrder = tuple[float, int]


def producer_order(job: dict[str, Any]) -> ProducerOrder:
    """Use local insertion order to break equal start-time ties deterministically."""
    return float(job["timestamp"]), int(job["id"])


def preceding_producer(
    producers: list[dict[str, Any]], before: ProducerOrder | None
) -> dict[str, Any] | None:
    """Choose the latest producer preceding the consuming job, when supplied."""
    eligible = (job for job in producers if before is None or producer_order(job) < before)
    return max(eligible, key=producer_order, default=None)
