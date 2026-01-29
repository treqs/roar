"""
Process summarizer service for provenance collection.

Summarizes process trees by collapsing fork-only duplicates.
"""

from typing import Any


class ProcessSummarizerService:
    """Summarizes process trees by collapsing fork-only duplicates."""

    def summarize(self, processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Summarize process tree by collapsing fork-only duplicates.

        A fork-only process is one where the child has the same command as its parent
        (no exec happened). These are collapsed into a fork_count on the parent.

        Args:
            processes: List of process dicts with pid, parent_pid, command

        Returns:
            Summarized process tree as a list of dicts with command, fork_count, children
        """
        if not processes:
            return []

        # Build lookup by pid
        by_pid: dict[int, dict[str, Any]] = {p["pid"]: p for p in processes}

        # Build parent -> children mapping
        children_of: dict[int, list[int]] = {}
        for p in processes:
            parent = p.get("parent_pid")
            if parent is not None:
                children_of.setdefault(parent, []).append(p["pid"])

        # Find root process(es) - those without a parent in our set
        roots = [
            p for p in processes if p.get("parent_pid") is None or p.get("parent_pid") not in by_pid
        ]

        # Summarize from roots
        summarized = []
        for root in roots:
            summary = self._summarize_node(root["pid"], by_pid, children_of)
            if summary:
                summarized.append(summary)

        return summarized

    def _commands_equal(self, cmd1: list[str] | None, cmd2: list[str] | None) -> bool:
        """Check if two command lists are identical."""
        if cmd1 is None or cmd2 is None:
            return False
        return cmd1 == cmd2

    def _summarize_node(
        self,
        pid: int,
        by_pid: dict[int, dict[str, Any]],
        children_of: dict[int, list[int]],
    ) -> dict[str, Any] | None:
        """
        Recursively summarize a process node.

        Args:
            pid: Process ID to summarize
            by_pid: Lookup dict of pid -> process
            children_of: Lookup dict of parent_pid -> child pids

        Returns:
            Summarized dict with command, fork_count (if >0), children (if any)
        """
        proc = by_pid.get(pid)
        if proc is None:
            return None

        command = proc.get("command", [])
        child_pids = children_of.get(pid, [])

        # Count fork-only children (same command, no exec)
        fork_only_count = 0
        exec_children: list[dict[str, Any]] = []

        for child_pid in child_pids:
            child = by_pid.get(child_pid)
            if child is None:
                continue

            child_command = child.get("command", [])

            if self._commands_equal(command, child_command):
                # Fork-only: same command as parent
                fork_only_count += 1
                # But this fork might have its own children that exec'd
                # We need to recurse into fork-only children too
                grandchild_pids = children_of.get(child_pid, [])
                for grandchild_pid in grandchild_pids:
                    grandchild = by_pid.get(grandchild_pid)
                    if grandchild:
                        grandchild_cmd = grandchild.get("command", [])
                        if self._commands_equal(command, grandchild_cmd):
                            # Also fork-only
                            fork_only_count += 1
                        else:
                            # Grandchild exec'd something different
                            child_summary = self._summarize_node(
                                grandchild_pid, by_pid, children_of
                            )
                            if child_summary:
                                exec_children.append(child_summary)
            else:
                # Child exec'd a different program
                child_summary = self._summarize_node(child_pid, by_pid, children_of)
                if child_summary:
                    exec_children.append(child_summary)

        # Build result
        result: dict[str, Any] = {"command": command}

        # Only include fork_count if > 0
        if fork_only_count > 0:
            result["fork_count"] = fork_only_count

        # Only include children if there are any
        if exec_children:
            result["children"] = exec_children

        return result
