from pathlib import Path
from unittest.mock import patch

from roar.application.reproduce.lookup import lookup_pipeline_result


def test_lookup_pipeline_result_prefers_local_and_skips_remote(tmp_path: Path) -> None:
    with (
        patch(
            "roar.application.reproduce.lookup._lookup_local", return_value={"pipeline": "local"}
        ),
        patch("roar.application.reproduce.lookup._lookup_remote") as lookup_remote,
    ):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url="http://example.test",
            glaas_client=None,
        )

    assert result.pipeline == {"pipeline": "local"}
    assert result.error is None
    assert result.source == "local"
    lookup_remote.assert_not_called()


def test_lookup_pipeline_result_returns_remote_result_on_local_miss(tmp_path: Path) -> None:
    with (
        patch("roar.application.reproduce.lookup._lookup_local", return_value=None),
        patch(
            "roar.application.reproduce.lookup._lookup_remote",
            return_value=({"pipeline": "remote"}, None),
        ),
    ):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url="http://example.test",
            glaas_client=None,
        )

    assert result.pipeline == {"pipeline": "remote"}
    assert result.error is None
    assert result.source == "remote"


def test_lookup_pipeline_result_returns_remote_error_on_local_miss(tmp_path: Path) -> None:
    with (
        patch("roar.application.reproduce.lookup._lookup_local", return_value=None),
        patch(
            "roar.application.reproduce.lookup._lookup_remote",
            return_value=(None, "permission denied"),
        ),
    ):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url="http://example.test",
            glaas_client=None,
        )

    assert result.pipeline is None
    assert result.error == "permission denied"
    assert result.source == "remote"


def test_lookup_pipeline_result_returns_not_found_when_remote_lookup_disabled(
    tmp_path: Path,
) -> None:
    with patch("roar.application.reproduce.lookup._lookup_local", return_value=None):
        result = lookup_pipeline_result(
            hash_prefix="a" * 64,
            roar_dir=tmp_path / ".roar",
            server_url=None,
            glaas_client=None,
        )

    assert result.pipeline is None
    assert "Artifact not found" in (result.error or "")
    assert result.source == "none"
