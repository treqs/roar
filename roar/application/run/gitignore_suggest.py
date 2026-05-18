"""Build user-facing ``.gitignore`` suggestion lines for a set of paths.

Both the start-of-run dirty-tree error and the end-of-run "your outputs
will block the next run" warning use these to advise the user on
exactly what to add to ``.gitignore``. The strategy:

* Group paths by extension.
* If ≥``_PATTERN_THRESHOLD`` paths share an extension, suggest
  ``echo '*.<ext>' >> .gitignore`` with a ``(covers N of M)``
  annotation. Saves the user from typing N near-identical lines and
  scales to large output sets without dumping a wall of paths.
* Stragglers (extensions with <``_PATTERN_THRESHOLD`` matches,
  extension-less files) get individual ``echo '<path>' >> .gitignore``
  lines.
* If literal-path lines would exceed ``_LITERAL_CAP`` after grouping,
  cap them and emit a final ``# and N more`` comment so output stays
  bounded even with very large output sets.

The function returns the suggestion lines as a list of strings —
callers decide indentation/styling.
"""

from __future__ import annotations

import os
from collections import defaultdict

_PATTERN_THRESHOLD = 3
_LITERAL_CAP = 8


def gitignore_lines(paths: list[str]) -> list[str]:
    """Return a list of suggested ``echo … >> .gitignore`` lines for ``paths``.

    Paths are expected to be relative to the repo root (or otherwise
    suitable to drop into ``.gitignore`` verbatim). Returns ``[]`` for
    an empty input.
    """
    if not paths:
        return []

    by_ext: dict[str, list[str]] = defaultdict(list)
    no_ext: list[str] = []
    for path in paths:
        ext = _extension(path)
        if ext:
            by_ext[ext].append(path)
        else:
            no_ext.append(path)

    total = len(paths)
    lines: list[str] = []
    used_paths: set[str] = set()

    # Pattern suggestions first — they collapse the largest groups.
    for ext, group in sorted(by_ext.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(group) >= _PATTERN_THRESHOLD:
            coverage = (
                f"(covers all {total})"
                if len(group) == total
                else f"(covers {len(group)} of {total})"
            )
            lines.append(f"echo '*{ext}' >> .gitignore     {coverage}")
            used_paths.update(group)

    # Literal lines for everything not covered by a pattern.
    stragglers = [p for p in paths if p not in used_paths]
    visible = stragglers[:_LITERAL_CAP]
    for path in visible:
        lines.append(f"echo '{path}' >> .gitignore")
    if len(stragglers) > _LITERAL_CAP:
        lines.append(f"# and {len(stragglers) - _LITERAL_CAP} more")

    return lines


def _extension(path: str) -> str:
    """Return the file extension (with leading dot) for grouping, or ``""``.

    ``os.path.splitext`` matches ``.gitignore``'s own semantics for
    ``*.<ext>`` patterns — both key off the final dot. Returns ``""``
    for extensionless files (which the caller handles as stragglers)
    and for dotfiles like ``.env`` (no useful pattern to suggest).
    """
    name = os.path.basename(path)
    if name.startswith(".") and name.count(".") == 1:
        return ""  # dotfile, no useful pattern
    _, ext = os.path.splitext(name)
    return ext
