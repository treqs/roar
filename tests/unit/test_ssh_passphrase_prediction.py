"""Tests for the SSH passphrase-prompt prediction cascade (task #46).

`roar register` pushes roar tags with `git push`, which inherits stdin — so a
passphrase-protected SSH key not loaded in ssh-agent prompts mid-register with
no context. `warn_if_ssh_passphrase_prompt_likely` predicts that cheaply:

  1. transport check  (is the remote SSH at all?)
  2. cheap local gate (could the chosen key need a passphrase, uncached?)
  3. BatchMode probe  (definitive — only when 1+2 say "maybe")

Everything fails open: any uncertainty stays quiet rather than blocking a push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import roar.application.publish.git_remote as gr

# ---------------------------------------------------------------------------
# ssh_host_from_url — transport detection / host extraction
# ---------------------------------------------------------------------------


class TestSshHostFromUrl:
    def test_scp_like_shorthand(self) -> None:
        assert gr.ssh_host_from_url("git@github.com:treqs/roar.git") == "github.com"

    def test_scp_like_preserves_config_alias(self) -> None:
        # An ssh config alias must survive so `ssh -G` resolves the real key.
        assert gr.ssh_host_from_url("git@my-alias:treqs/roar.git") == "my-alias"

    def test_ssh_scheme_with_user_and_port(self) -> None:
        assert gr.ssh_host_from_url("ssh://git@example.com:2222/treqs/roar.git") == "example.com"

    def test_ssh_scheme_without_user(self) -> None:
        assert gr.ssh_host_from_url("ssh://example.com/treqs/roar.git") == "example.com"

    def test_https_is_not_ssh(self) -> None:
        assert gr.ssh_host_from_url("https://github.com/treqs/roar.git") is None

    def test_git_scheme_is_not_ssh(self) -> None:
        assert gr.ssh_host_from_url("git://github.com/treqs/roar.git") is None

    def test_file_scheme_is_not_ssh(self) -> None:
        assert gr.ssh_host_from_url("file:///srv/git/roar.git") is None

    def test_local_absolute_path_is_not_ssh(self) -> None:
        assert gr.ssh_host_from_url("/tmp/bare-remote") is None

    def test_local_relative_path_is_not_ssh(self) -> None:
        assert gr.ssh_host_from_url("../bare-remote") is None

    def test_empty_is_none(self) -> None:
        assert gr.ssh_host_from_url("") is None


# ---------------------------------------------------------------------------
# warn_if_ssh_passphrase_prompt_likely — cascade orchestration + cost gating
# ---------------------------------------------------------------------------


class TestWarnOrchestration:
    def test_non_ssh_remote_skips_everything(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "remote_push_url", lambda root, remote: "https://x/y.git")
        probed = {"called": False}
        monkeypatch.setattr(
            gr,
            "_batchmode_push_would_block",
            lambda root, remote: probed.__setitem__("called", True) or True,
        )

        assert gr.warn_if_ssh_passphrase_prompt_likely(Path("/repo"), "origin") is None
        assert probed["called"] is False  # no probe for non-SSH

    def test_unresolvable_url_stays_quiet(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "remote_push_url", lambda root, remote: None)
        assert gr.warn_if_ssh_passphrase_prompt_likely(Path("/repo"), "origin") is None

    def test_cheap_gate_false_skips_probe(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "remote_push_url", lambda root, remote: "git@host:r.git")
        monkeypatch.setattr(gr, "_passphrase_prompt_plausible", lambda host: False)
        probed = {"called": False}
        monkeypatch.setattr(
            gr,
            "_batchmode_push_would_block",
            lambda root, remote: probed.__setitem__("called", True) or True,
        )

        assert gr.warn_if_ssh_passphrase_prompt_likely(Path("/repo"), "origin") is None
        assert probed["called"] is False  # keys unencrypted/cached → no network probe

    def test_probe_authenticates_silently_no_warning(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "remote_push_url", lambda root, remote: "git@host:r.git")
        monkeypatch.setattr(gr, "_passphrase_prompt_plausible", lambda host: True)
        monkeypatch.setattr(gr, "_batchmode_push_would_block", lambda root, remote: False)

        assert gr.warn_if_ssh_passphrase_prompt_likely(Path("/repo"), "origin") is None

    def test_probe_would_block_returns_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "remote_push_url", lambda root, remote: "git@host:r.git")
        monkeypatch.setattr(gr, "_passphrase_prompt_plausible", lambda host: True)
        monkeypatch.setattr(gr, "_batchmode_push_would_block", lambda root, remote: True)

        message = gr.warn_if_ssh_passphrase_prompt_likely(Path("/repo"), "origin")
        assert message is not None
        assert "passphrase" in message
        assert "ssh-add" in message
        assert "origin" in message


# ---------------------------------------------------------------------------
# _passphrase_prompt_plausible — cheap local gate
# ---------------------------------------------------------------------------


class TestPassphrasePlausible:
    def test_no_introspectable_keys_defers_to_probe(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "_ssh_identity_files", lambda host: [])
        assert gr._passphrase_prompt_plausible("host") is True

    def test_unencrypted_key_is_not_plausible(self, monkeypatch) -> None:
        key = Path("/home/u/.ssh/id_ed25519")
        monkeypatch.setattr(gr, "_ssh_identity_files", lambda host: [key])
        monkeypatch.setattr(gr, "_agent_fingerprints", lambda: set())
        monkeypatch.setattr(gr, "_key_fingerprint", lambda p: "SHA256:abc")
        monkeypatch.setattr(gr, "_key_is_encrypted", lambda p: False)

        assert gr._passphrase_prompt_plausible("host") is False

    def test_encrypted_uncached_key_is_plausible(self, monkeypatch) -> None:
        key = Path("/home/u/.ssh/id_rsa")
        monkeypatch.setattr(gr, "_ssh_identity_files", lambda host: [key])
        monkeypatch.setattr(gr, "_agent_fingerprints", lambda: set())
        monkeypatch.setattr(gr, "_key_fingerprint", lambda p: "SHA256:enc")
        monkeypatch.setattr(gr, "_key_is_encrypted", lambda p: True)

        assert gr._passphrase_prompt_plausible("host") is True

    def test_encrypted_but_cached_key_is_not_plausible(self, monkeypatch) -> None:
        key = Path("/home/u/.ssh/id_rsa")
        monkeypatch.setattr(gr, "_ssh_identity_files", lambda host: [key])
        monkeypatch.setattr(gr, "_agent_fingerprints", lambda: {"SHA256:cached"})
        monkeypatch.setattr(gr, "_key_fingerprint", lambda p: "SHA256:cached")
        monkeypatch.setattr(gr, "_key_is_encrypted", lambda p: True)

        assert gr._passphrase_prompt_plausible("host") is False

    def test_undeterminable_encryption_counts_as_plausible(self, monkeypatch) -> None:
        key = Path("/home/u/.ssh/weird_key")
        monkeypatch.setattr(gr, "_ssh_identity_files", lambda host: [key])
        monkeypatch.setattr(gr, "_agent_fingerprints", lambda: set())
        monkeypatch.setattr(gr, "_key_fingerprint", lambda p: None)
        monkeypatch.setattr(gr, "_key_is_encrypted", lambda p: None)

        assert gr._passphrase_prompt_plausible("host") is True


# ---------------------------------------------------------------------------
# _run_command — best-effort, fails open
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_missing_binary_returns_none(self) -> None:
        assert gr._run_command(["definitely-not-a-real-binary-xyz", "--help"]) is None

    def test_timeout_returns_none(self, monkeypatch) -> None:
        def _boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr(gr.shutil, "which", lambda name: "/usr/bin/x")
        monkeypatch.setattr(gr.subprocess, "run", _boom)
        assert gr._run_command(["x"], timeout=1) is None


# ---------------------------------------------------------------------------
# _batchmode_push_would_block — probe env + result mapping
# ---------------------------------------------------------------------------


class TestBatchModeProbe:
    def test_clean_exit_means_no_block(self, monkeypatch) -> None:
        captured: dict = {}

        def _fake_run(args, *, cwd=None, env=None, timeout=None):
            captured["env"] = env
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gr, "_run_command", _fake_run)
        assert gr._batchmode_push_would_block("/repo", "origin") is False
        assert captured["env"]["GIT_SSH_COMMAND"].startswith("ssh -o BatchMode=yes")
        assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_nonzero_exit_means_would_block(self, monkeypatch) -> None:
        monkeypatch.setattr(
            gr,
            "_run_command",
            lambda *a, **k: subprocess.CompletedProcess(a, returncode=255, stdout="", stderr="x"),
        )
        assert gr._batchmode_push_would_block("/repo", "origin") is True

    def test_unrunnable_probe_does_not_cry_wolf(self, monkeypatch) -> None:
        monkeypatch.setattr(gr, "_run_command", lambda *a, **k: None)
        assert gr._batchmode_push_would_block("/repo", "origin") is False
