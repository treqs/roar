#!/usr/bin/env python3
"""Viewer for roar tracer .msgpack binary files."""

import argparse
import datetime
import json
import sys

import msgpack


def load(path: str) -> dict:
    with open(path, "rb") as f:
        return msgpack.unpack(f, raw=False)


def fmt_time(ts: float) -> str:
    if not ts:
        return "N/A"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def print_report(data: dict, *, show_env: bool, show_process_env: bool) -> None:
    version = data.get("version", "?")
    mode = data.get("tracer_mode", "unknown")
    start = data.get("start_time", 0)
    end = data.get("end_time", 0)
    duration = end - start if start and end else 0
    dropped = data.get("events_dropped")
    chunk_size = data.get("chunk_size")

    print(f"Tracer Report  (version {version}, mode: {mode})")
    print(f"  Start:    {fmt_time(start)}")
    print(f"  End:      {fmt_time(end)}")
    print(f"  Duration: {duration:.3f}s")
    if chunk_size is not None:
        print(f"  Chunk size: {chunk_size}")
    if dropped:
        print(f"  Events dropped: {dropped}")
    print()

    # --- Processes ---
    processes = data.get("processes", [])
    print(f"Processes ({len(processes)})")
    for proc in processes:
        pid = proc.get("pid", "?")
        ppid = proc.get("parent_pid")
        cmd = " ".join(proc.get("command", []))
        parent = f"  (parent {ppid})" if ppid is not None else ""
        print(f"  PID {pid}{parent}: {cmd}")
        if show_process_env:
            env = proc.get("env", {})
            if env:
                for k, v in sorted(env.items()):
                    print(f"    {k}={v}")
    print()

    # --- Files ---
    files = data.get("files", [])
    opened = data.get("opened_files", [])
    read_files = data.get("read_files", [])
    written_files = data.get("written_files", [])

    if files:
        read_list = [f for f in files if f.get("read")]
        write_list = [f for f in files if f.get("written")]
        read_only = [f for f in files if f.get("read") and not f.get("written")]
        write_only = [f for f in files if f.get("written") and not f.get("read")]
        read_write = [f for f in files if f.get("read") and f.get("written")]
        open_only = [f for f in files if not f.get("read") and not f.get("written")]

        print(f"Files ({len(files)} total: {len(read_list)} read, {len(write_list)} written)")

        def print_file_list(label: str, items: list[dict]) -> None:
            if not items:
                return
            print(f"\n  {label} ({len(items)})")
            for rec in sorted(items, key=lambda r: r.get("path", "")):
                path = rec.get("path", "?")
                extras = []
                cr = rec.get("chunks_read")
                cw = rec.get("chunks_written")
                if cr:
                    extras.append(f"{len(cr)} chunks read")
                if cw:
                    extras.append(f"{len(cw)} chunks written")
                suffix = f"  [{', '.join(extras)}]" if extras else ""
                print(f"    {path}{suffix}")

        print_file_list("Read-only", read_only)
        print_file_list("Write-only", write_only)
        print_file_list("Read+Write", read_write)
        print_file_list("Opened (no I/O)", open_only)
    else:
        # Legacy format — flat lists only
        print("Files (legacy format)")
        if opened:
            print(f"\n  Opened ({len(opened)})")
            for p in sorted(opened):
                print(f"    {p}")
        if read_files:
            print(f"\n  Read ({len(read_files)})")
            for p in sorted(read_files):
                print(f"    {p}")
        if written_files:
            print(f"\n  Written ({len(written_files)})")
            for p in sorted(written_files):
                print(f"    {p}")
    print()

    # --- Env accessed ---
    env_accessed = data.get("env_accessed", {})
    if env_accessed and show_env:
        print(f"Environment Variables Accessed ({len(env_accessed)})")
        for k, v in sorted(env_accessed.items()):
            print(f"  {k}={v}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="View roar tracer .msgpack files",
    )
    parser.add_argument("file", help="Path to .msgpack tracer output file")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Dump raw data as JSON instead of formatted output",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="Show environment variables accessed during tracing",
    )
    parser.add_argument(
        "--process-env",
        action="store_true",
        help="Show full environment for each process",
    )
    args = parser.parse_args()

    try:
        data = load(args.file)
    except FileNotFoundError:
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    except msgpack.exceptions.UnpackException as e:
        print(f"Error: failed to parse msgpack: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print_report(data, show_env=args.env, show_process_env=args.process_env)


if __name__ == "__main__":
    main()
