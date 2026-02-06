"""
Integration test for false positive write detection.

Verifies that files opened for writing but with 0 bytes written
are NOT recorded as outputs.

Also verifies that fd reuse after close() does not cause false positive
write attribution to previously-tracked repo files.
"""

import platform
import sqlite3
import sys
import textwrap

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


def test_read_only_repo_file_not_recorded_as_output(temp_git_repo, roar_cli, git_commit):
    """
    A repo file opened only for reading should NOT be recorded as an output,
    even when its fd number is later reused by pipe() and written to.

    Note: pipe() may or may not reuse the exact fd number freed by close(),
    so this test may not always trigger the fd-reuse bug. See the dup2 variant
    (test_repo_file_read_then_pipe_write_not_attributed) for a deterministic
    reproducer.

    Bug mechanism (tracer/src/main.rs line 349):
    1. Script opens repo file for reading -> tracer records fd N -> path
    2. Script closes the file -> tracer does NOT remove fd N from fd_table
    3. os.pipe() creates new fds, one of which may reuse fd N
    4. Writing to the pipe fd N is falsely attributed to the repo file
    5. The repo file appears in job_outputs as "written"
    """
    # Create a data file in the repo and commit it
    data_file = temp_git_repo / "data.csv"
    data_file.write_text("id,name,value\n1,alice,100\n2,bob,200\n")
    git_commit("Add data.csv")

    # Create a script that reads the repo file, closes it, then uses pipe()
    # which is likely to reuse the freed fd number
    script = temp_git_repo / "read_and_pipe.py"
    script.write_text(
        textwrap.dedent("""\
        import os

        # Step 1: Open the repo file for reading and note the fd
        fd = os.open("data.csv", os.O_RDONLY)
        data = os.read(fd, 4096)
        print(f"Read {len(data)} bytes from data.csv on fd {fd}")

        # Step 2: Close the file - tracer's fd_table still maps fd -> data.csv
        os.close(fd)

        # Step 3: Create a pipe - one end likely reuses the old fd number
        read_end, write_end = os.pipe()
        print(f"Pipe fds: read={read_end}, write={write_end}")

        # Step 4: Write to the pipe - if write_end or read_end reused fd,
        # the tracer may falsely attribute this write to data.csv
        os.write(write_end, b"pipe data that should not be attributed to data.csv\\n")
        os.close(write_end)
        os.close(read_end)

        # Step 5: Also create a genuine output file as a sanity check
        with open("output.txt", "w") as f:
            f.write("this is real output\\n")

        print("Script completed")
    """)
    )
    git_commit("Add read_and_pipe script")

    # Run the script with roar
    result = roar_cli("run", sys.executable, "read_and_pipe.py", check=False)
    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # Query the database for job outputs
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

        # Check job_outputs
        cur.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job_id,))
        output_paths = [row["path"] for row in cur.fetchall()]

        # Sanity check: output.txt SHOULD be recorded as output
        output_txt = [p for p in output_paths if "output.txt" in p]
        assert len(output_txt) > 0, (
            f"output.txt was NOT recorded as output but should have been. "
            f"Output paths: {output_paths}"
        )

        # Bug check: data.csv should NOT be in outputs (it was only read)
        data_csv_outputs = [p for p in output_paths if "data.csv" in p]
        assert len(data_csv_outputs) == 0, (
            f"data.csv was incorrectly recorded as output (false positive from fd reuse). "
            f"Output paths: {output_paths}"
        )
    finally:
        conn.close()


def test_repo_file_read_then_pipe_write_not_attributed(temp_git_repo, roar_cli, git_commit):
    """
    Deterministic fd reuse test: opens a repo file at a low fd, closes it,
    then uses dup2() to place a pipe write end on the same fd number.

    Verifies the tracer cleans up fd_table on close() so writes to the
    reused fd are not falsely attributed to the original repo file.
    """
    # Create a repo file and commit
    repo_file = temp_git_repo / "config.json"
    repo_file.write_text('{"key": "value"}\n')
    git_commit("Add config.json")

    # Script that forces deterministic fd reuse with dup2
    script = temp_git_repo / "forced_fd_reuse.py"
    script.write_text(
        textwrap.dedent("""\
        import os

        # Step 1: Open repo file for reading
        repo_fd = os.open("config.json", os.O_RDONLY)
        content = os.read(repo_fd, 4096)
        print(f"Read config.json on fd {repo_fd}: {len(content)} bytes")

        # Step 2: Close it
        os.close(repo_fd)

        # Step 3: Create a pipe
        r, w = os.pipe()
        print(f"Pipe fds: read={r}, write={w}")

        # Step 4: The pipe read end may have landed on repo_fd (kernel reuses
        # lowest available fd). Move it out of the way before dup2 clobbers it,
        # otherwise dup2 closes the read end and we get BrokenPipeError.
        if r == repo_fd:
            saved_r = os.dup(r)
            os.close(r)
            r = saved_r

        # Step 5: Use dup2 to force the write end onto the old repo_fd number
        os.dup2(w, repo_fd)
        if w != repo_fd:
            os.close(w)
        print(f"dup2'd pipe write end to fd {repo_fd}")

        # Step 6: Write to the reused fd
        os.write(repo_fd, b"pipe data via reused fd\\n")

        # Drain the pipe so no broken pipe on close
        os.read(r, 1024)
        os.close(repo_fd)
        os.close(r)

        # Step 7: Create a genuine output as sanity check
        with open("result.txt", "w") as f:
            f.write("genuine output\\n")

        print("Script completed")
    """)
    )
    git_commit("Add forced fd reuse script")

    # Run the script with roar
    result = roar_cli("run", sys.executable, "forced_fd_reuse.py", check=False)
    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # Query the database for job outputs
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

        # Check job_outputs
        cur.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job_id,))
        output_paths = [row["path"] for row in cur.fetchall()]

        # Sanity check: result.txt SHOULD be recorded
        result_txt = [p for p in output_paths if "result.txt" in p]
        assert len(result_txt) > 0, (
            f"result.txt was NOT recorded as output but should have been. "
            f"Output paths: {output_paths}"
        )

        # Bug check: config.json should NOT be in outputs (only read, never written)
        config_outputs = [p for p in output_paths if "config.json" in p]
        assert len(config_outputs) == 0, (
            f"config.json was incorrectly recorded as output (false positive from fd reuse via dup2). "
            f"Output paths: {output_paths}"
        )
    finally:
        conn.close()
