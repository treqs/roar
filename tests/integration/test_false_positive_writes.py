"""
Integration test for false positive write detection.

Verifies that files opened for writing but with 0 bytes written
are NOT recorded as outputs.
"""

import platform
import sqlite3
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="ptrace tracer is Linux-only"),
]


def test_zero_byte_write_not_recorded_as_output(temp_git_repo, roar_cli, git_commit):
    """
    Opening a file for write but writing 0 bytes should NOT record it as output.

    This tests the false positive write detection fix in the tracer.
    The tracer should only count a file as "written" if actual bytes were written.
    """
    # Create a test file that will be opened but not written to
    test_file = temp_git_repo / "data.txt"
    test_file.write_text("original content\n")
    git_commit("Add data file")

    # Create a script that calls write() but with an empty string (0 bytes)
    script = temp_git_repo / "noop_write.py"
    script.write_text("""
import sys
import os

# Open file for write and explicitly write 0 bytes
with open("data.txt", "w") as f:
    # This calls the write() syscall with count=0
    f.write("")
    f.flush()  # Ensure syscall happens
    # Also try os.write directly
    os.write(f.fileno(), b"")

print("Script completed")
""")
    git_commit("Add noop write script")

    # Run the script with roar
    result = roar_cli("run", sys.executable, "noop_write.py", check=False)
    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # Verify the job was recorded
    db_path = temp_git_repo / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        # Get the latest job
        cur.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
        job_row = cur.fetchone()
        assert job_row is not None, "No job found in the database"
        job_id = job_row["id"]

        # Check job_outputs - data.txt should NOT be there
        cur.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job_id,))
        output_paths = [row["path"] for row in cur.fetchall()]

        # The file should NOT be in outputs since no bytes were written
        data_txt_outputs = [p for p in output_paths if "data.txt" in p]
        assert len(data_txt_outputs) == 0, (
            f"data.txt was incorrectly recorded as output (false positive write). "
            f"Output paths: {output_paths}"
        )
    finally:
        conn.close()


def test_actual_write_is_recorded(temp_git_repo, roar_cli, git_commit):
    """
    Sanity check: actual writes SHOULD be recorded as outputs.
    """
    # Create a script that writes actual content
    script = temp_git_repo / "real_write.py"
    script.write_text("""
# Write actual content
with open("output.txt", "w") as f:
    f.write("hello world\\n")

print("Script completed")
""")
    git_commit("Add real write script")

    # Run the script with roar
    result = roar_cli("run", sys.executable, "real_write.py", check=False)
    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    git_commit("After write script")

    # Verify the job was recorded with output
    db_path = temp_git_repo / ".roar" / "roar.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        # Get the latest job
        cur.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
        job_row = cur.fetchone()
        assert job_row is not None, "No job found in the database"
        job_id = job_row["id"]

        # Check job_outputs - output.txt SHOULD be there
        cur.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job_id,))
        output_paths = [row["path"] for row in cur.fetchall()]

        # The file SHOULD be in outputs since bytes were written
        output_txt = [p for p in output_paths if "output.txt" in p]
        assert len(output_txt) > 0, (
            f"output.txt was NOT recorded as output but should have been. "
            f"Output paths: {output_paths}"
        )
    finally:
        conn.close()


def test_truncate_without_write_not_recorded(temp_git_repo, roar_cli, git_commit):
    """
    Opening with 'w' mode truncates the file but if no write() is called,
    the file should NOT be recorded as an output.

    This is a subtle case: the file IS modified (truncated to empty),
    but no write syscall with bytes > 0 occurred.
    """
    # Create a test file with content
    test_file = temp_git_repo / "truncate_me.txt"
    test_file.write_text("this will be truncated\n")
    git_commit("Add file to truncate")

    # Create a script that truncates but doesn't write
    script = temp_git_repo / "truncate_only.py"
    script.write_text("""
# Open for write (truncates) but don't write
f = open("truncate_me.txt", "w")
f.close()

print("Script completed")
""")
    git_commit("Add truncate script")

    # Run the script with roar
    result = roar_cli("run", sys.executable, "truncate_only.py", check=False)
    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # Verify the file was truncated
    assert test_file.stat().st_size == 0, "File was not truncated"

    # Verify the job was recorded
    db_path = temp_git_repo / ".roar" / "roar.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        # Get the latest job
        cur.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
        job_row = cur.fetchone()
        assert job_row is not None, "No job found in the database"
        job_id = job_row["id"]

        # Check job_outputs - truncate_me.txt should NOT be there
        # (even though the file was modified by truncation)
        cur.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job_id,))
        output_paths = [row["path"] for row in cur.fetchall()]

        truncate_outputs = [p for p in output_paths if "truncate_me.txt" in p]
        assert len(truncate_outputs) == 0, (
            f"truncate_me.txt was incorrectly recorded as output. Output paths: {output_paths}"
        )
    finally:
        conn.close()
