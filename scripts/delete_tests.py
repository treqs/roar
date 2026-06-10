#!/usr/bin/env python3
"""Remove specific test functions from test files using AST analysis."""

import ast
import sys
from pathlib import Path


def remove_functions(source: str, names_to_delete: set[str]) -> tuple[str, list[str]]:
    """Remove top-level and class-method test functions by name. Returns (new_source, removed)."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    # Collect (start_line, end_line) for each node to remove (1-indexed, inclusive)
    regions: list[tuple[int, int]] = []
    removed: list[str] = []

    nodes = list(ast.walk(tree))
    # Include module-level and class-level functions
    for node in nodes:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names_to_delete
        ):
            # Find the actual end including decorators
            start = node.lineno
            # Check for decorators above
            if node.decorator_list:
                start = node.decorator_list[0].lineno
            end = node.end_lineno
            regions.append((start, end))
            removed.append(node.name)

    if not regions:
        return source, []

    # Sort by start line descending so we can delete without offset issues
    regions.sort(key=lambda r: r[0], reverse=True)

    for start, end in regions:
        # Also consume a blank line immediately after the block if present
        after = end  # 0-indexed index of line after block
        while after < len(lines) and lines[after].strip() == "":
            after += 1
        # Keep at most 1 blank line before next content; remove the block
        del lines[start - 1 : after]

    return "".join(lines), removed


def process(spec: dict[str, list[str]], repo_root: Path) -> dict[str, list[str]]:
    """Apply deletions. Returns {file: [actually_removed]}."""
    results: dict[str, list[str]] = {}
    for rel_path, test_names in spec.items():
        path = repo_root / rel_path
        if not path.exists():
            print(f"SKIP (not found): {rel_path}", file=sys.stderr)
            continue
        source = path.read_text()
        new_source, removed = remove_functions(source, set(test_names))
        if removed:
            path.write_text(new_source)
            results[rel_path] = removed
            print(f"  removed {len(removed)}/{len(test_names)} from {rel_path}")
        else:
            missing = set(test_names)
            print(f"  WARNING: none of {missing} found in {rel_path}", file=sys.stderr)
    return results


if __name__ == "__main__":
    import json

    spec_file = Path(sys.argv[1])
    repo_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
    spec = json.loads(spec_file.read_text())
    results = process(spec, repo_root)
    total = sum(len(v) for v in results.values())
    print(f"\nTotal removed: {total}")
