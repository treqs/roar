"""Pure utilities for constructing roar Ray agent names. No ray dependency."""

from __future__ import annotations


def build_node_agent_name(job_id: str, node_id: str) -> str:
    return f"roar-node-agent-{job_id}-{str(node_id)[:8]}"
