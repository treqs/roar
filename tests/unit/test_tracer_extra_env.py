"""Tests for TracerService extra_env handling."""

from unittest.mock import MagicMock, patch

from roar.services.execution.tracer import TracerService


def _make_signal_handler():
    handler = MagicMock()
    handler.is_interrupted.return_value = False
    return handler


class TestTracerExtraEnv:
    def test_extra_env_vars_appear_in_popen_env(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        svc._get_tracer_candidates = MagicMock(return_value=[("ptrace", "/fake/roar-tracer")])

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 12345

        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("roar.config.load_config", return_value={}),
        ):
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
                extra_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"},
            )

        popen_kwargs = mock_popen.call_args.kwargs
        assert popen_kwargs["env"]["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9090"

    def test_extra_env_overwrites_existing_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")

        svc = TracerService(package_path=tmp_path / "roar")
        svc._get_tracer_candidates = MagicMock(return_value=[("ptrace", "/fake/roar-tracer")])

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 12345

        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("roar.config.load_config", return_value={}),
        ):
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
                extra_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"},
            )

        popen_kwargs = mock_popen.call_args.kwargs
        # The proxy URL should overwrite the original
        assert popen_kwargs["env"]["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9090"

    def test_extra_env_none_preserves_original(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_VAR", "original")

        svc = TracerService(package_path=tmp_path / "roar")
        svc._get_tracer_candidates = MagicMock(return_value=[("ptrace", "/fake/roar-tracer")])

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 12345

        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("roar.config.load_config", return_value={}),
        ):
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
                extra_env=None,
            )

        popen_kwargs = mock_popen.call_args.kwargs
        assert popen_kwargs["env"]["MY_VAR"] == "original"

    def test_extra_env_does_not_affect_unrelated_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNRELATED_VAR", "should_remain")

        svc = TracerService(package_path=tmp_path / "roar")
        svc._get_tracer_candidates = MagicMock(return_value=[("ptrace", "/fake/roar-tracer")])

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 12345

        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("roar.config.load_config", return_value={}),
        ):
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
                extra_env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9090"},
            )

        popen_kwargs = mock_popen.call_args.kwargs
        assert popen_kwargs["env"]["UNRELATED_VAR"] == "should_remain"
        assert popen_kwargs["env"]["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9090"
