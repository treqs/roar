"""Tests for ``gitignore_lines`` — the .gitignore suggestion builder.

Strategy under test:

* Single path → one literal ``echo '<path>' >> .gitignore`` line.
* ≥3 paths sharing an extension → one ``echo '*.<ext>' >> .gitignore``
  line with a ``(covers N of M)`` annotation. Saves the user from
  typing N near-identical lines and scales to large output sets.
* Stragglers (extensions below the threshold, extensionless files,
  dotfiles) get individual lines.
* Literal lines cap at 8; the rest collapses to a comment.
"""

from __future__ import annotations

from roar.application.run.gitignore_suggest import gitignore_lines


def test_empty_input_returns_empty() -> None:
    assert gitignore_lines([]) == []


def test_single_path_literal_only() -> None:
    assert gitignore_lines(["model.pkl"]) == ["echo 'model.pkl' >> .gitignore"]


def test_below_threshold_stays_literal() -> None:
    """Two .pkl files (below the 3-file threshold) get individual lines."""
    lines = gitignore_lines(["a.pkl", "b.pkl"])
    assert lines == [
        "echo 'a.pkl' >> .gitignore",
        "echo 'b.pkl' >> .gitignore",
    ]


def test_three_same_extension_triggers_pattern() -> None:
    lines = gitignore_lines(["a.pkl", "b.pkl", "c.pkl"])
    assert len(lines) == 1
    assert "echo '*.pkl' >> .gitignore" in lines[0]
    assert "(covers all 3)" in lines[0]


def test_mixed_extensions_pattern_covers_majority_stragglers_literal() -> None:
    paths = ["m0.pkl", "m1.pkl", "m2.pkl", "metrics.json", "plot.png"]
    lines = gitignore_lines(paths)
    # Pattern for .pkl (3 of 5)
    pkl_line = next(line for line in lines if "*.pkl" in line)
    assert "(covers 3 of 5)" in pkl_line
    # Literals for the others
    assert any("metrics.json" in line for line in lines)
    assert any("plot.png" in line for line in lines)


def test_multiple_patterns_when_multiple_groups_hit_threshold() -> None:
    paths = ["a.pkl", "b.pkl", "c.pkl", "x.png", "y.png", "z.png"]
    lines = gitignore_lines(paths)
    pkl_lines = [line for line in lines if "*.pkl" in line]
    png_lines = [line for line in lines if "*.png" in line]
    assert len(pkl_lines) == 1
    assert len(png_lines) == 1
    # Both patterns annotated `(covers 3 of 6)`
    assert "(covers 3 of 6)" in pkl_lines[0]
    assert "(covers 3 of 6)" in png_lines[0]


def test_extensionless_paths_get_literal_lines() -> None:
    lines = gitignore_lines(["Makefile_out", "checkpoint"])
    assert "echo 'Makefile_out' >> .gitignore" in lines
    assert "echo 'checkpoint' >> .gitignore" in lines


def test_dotfile_no_pattern_suggested() -> None:
    """`.env` is a dotfile — no `*.env` pattern (that would also catch
    files like `local.env` which the user may want)."""
    lines = gitignore_lines([".env"])
    assert lines == ["echo '.env' >> .gitignore"]


def test_literal_cap_collapses_excess_stragglers() -> None:
    paths = [f"f{i}.txt" for i in range(2)] + [f"u{i}.unique{i}" for i in range(15)]
    lines = gitignore_lines(paths)
    # Up to 8 literal lines, then `# and N more`.
    literal_lines = [line for line in lines if line.startswith("echo ")]
    assert len(literal_lines) == 8
    assert any(line.startswith("# and") for line in lines)


def test_all_under_pattern_emits_no_stragglers() -> None:
    """When the pattern covers every path, no literal lines remain."""
    lines = gitignore_lines(["a.pkl", "b.pkl", "c.pkl", "d.pkl"])
    assert len(lines) == 1
    assert "*.pkl" in lines[0]
