from __future__ import annotations

import importlib
import json
from pathlib import Path


def _module():
    return importlib.import_module("roar.cli.commands._ray_job_submit")


def _base_ray_job_submit_command() -> list[str]:
    return [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--",
        "python",
        "main.py",
    ]


def _runtime_env_json(command: list[str]) -> dict:
    for index, arg in enumerate(command):
        if arg == "--runtime-env-json" and index + 1 < len(command):
            return json.loads(command[index + 1])
        if arg.startswith("--runtime-env-json="):
            return json.loads(arg.split("=", 1)[1])
    raise AssertionError("expected --runtime-env-json in rewritten command")


def _fixed_fragment_key() -> dict[str, str]:
    return {
        "session_id": "8f8cd1a7-57be-4543-8a7b-7befef09ee2a",
        "token": "a" * 64,
        "token_hash": "b" * 64,
        "created_at": "2026-03-04T00:00:00+00:00",
    }


def _rewrite_with_fragment_session(monkeypatch, tmp_path: Path):
    module = _module()
    register_calls: list[tuple[str, str, str, int]] = []

    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: "http://localhost:3001")
    monkeypatch.setattr(module, "generate_fragment_key", _fixed_fragment_key, raising=False)

    def _fake_register(glaas_url: str, session_id: str, token_hash: str, ttl: int = 86400) -> None:
        register_calls.append((glaas_url, session_id, token_hash, ttl))

    monkeypatch.setattr(module, "_register_fragment_session", _fake_register, raising=False)
    monkeypatch.chdir(tmp_path)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())
    return module, rewritten, register_calls


def test_maybe_rewrite_preregisters_fragment_session_when_glaas_url_set(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, _, register_calls = _rewrite_with_fragment_session(monkeypatch, tmp_path)
    key = _fixed_fragment_key()

    assert register_calls == [
        ("http://localhost:3001", key["session_id"], key["token_hash"], 86400),
    ]


def test_maybe_rewrite_injects_roar_session_id_into_runtime_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, rewritten, _ = _rewrite_with_fragment_session(monkeypatch, tmp_path)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"]["ROAR_SESSION_ID"] == _fixed_fragment_key()["session_id"]
    assert rewritten.session_id == _fixed_fragment_key()["session_id"]


def test_maybe_rewrite_injects_roar_fragment_token_into_runtime_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, rewritten, _ = _rewrite_with_fragment_session(monkeypatch, tmp_path)

    runtime_env = _runtime_env_json(rewritten.command)
    assert runtime_env["env_vars"]["ROAR_FRAGMENT_TOKEN"] == _fixed_fragment_key()["token"]
    assert rewritten.session_id == _fixed_fragment_key()["session_id"]


def test_maybe_rewrite_saves_key_file_to_roar_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, _, _ = _rewrite_with_fragment_session(monkeypatch, tmp_path)
    key = _fixed_fragment_key()
    key_path = tmp_path / ".roar" / "fragment-sessions" / f"{key['session_id']}.key"

    assert key_path.exists()
    payload = json.loads(key_path.read_text(encoding="utf-8"))
    assert payload["session_id"] == key["session_id"]
    assert payload["token"] == key["token"]


def test_maybe_rewrite_skips_fragment_session_when_no_glaas_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _module()
    register_calls: list[tuple[str, str, str, int]] = []

    monkeypatch.setattr(module, "_resolve_roar_requirement", lambda: "roar-cli==1.0.0")
    monkeypatch.setattr(module, "_resolve_glaas_url", lambda: None)
    monkeypatch.setattr(module, "generate_fragment_key", _fixed_fragment_key, raising=False)

    def _fake_register(glaas_url: str, session_id: str, token_hash: str, ttl: int = 86400) -> None:
        register_calls.append((glaas_url, session_id, token_hash, ttl))

    monkeypatch.setattr(module, "_register_fragment_session", _fake_register, raising=False)
    monkeypatch.chdir(tmp_path)

    rewritten = module.maybe_rewrite_ray_job_submit(_base_ray_job_submit_command())

    assert register_calls == []
    assert not (tmp_path / ".roar" / "fragment-sessions").exists()
    assert rewritten.session_id is None
