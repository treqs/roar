from __future__ import annotations

from unittest.mock import MagicMock

from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext
from roar.services.registration.secrets import detect_lineage_secrets, filter_lineage_secrets


def test_detect_lineage_secrets_scans_git_command_and_string_metadata() -> None:
    omit_filter = MagicMock()
    omit_filter.detect_secrets.side_effect = [
        ["git-secret"],
        ["command-secret"],
        ["metadata-secret"],
    ]
    omit_filter.get_detection_summary.return_value = ["git-secret", "command-secret"]

    result = detect_lineage_secrets(
        lineage=LineageData(
            jobs=[
                {
                    "command": "SECRET=1 python train.py",
                    "metadata": "token=abc",
                }
            ],
            artifacts=[],
            artifact_hashes=set(),
            pipeline=None,
        ),
        git_context=GitContext(repo="https://token@example.com/repo.git", branch="main", commit="deadbeef"),
        omit_filter=omit_filter,
    )

    assert result == ["git-secret", "command-secret"]


def test_filter_lineage_secrets_redacts_command_and_metadata() -> None:
    omit_filter = MagicMock()
    omit_filter.filter_command.return_value = ("python train.py", ["cmd"])
    omit_filter.filter_telemetry.return_value = ("{}", ["telemetry"])
    omit_filter.filter_metadata.return_value = ({"safe": True}, ["metadata"])

    result = filter_lineage_secrets(
        lineage=LineageData(
            jobs=[
                {
                    "command": "SECRET=1 python train.py",
                    "metadata": "token=abc",
                },
                {
                    "command": "python eval.py",
                    "metadata": {"token": "abc"},
                },
            ],
            artifacts=[],
            artifact_hashes=set(),
            pipeline=None,
        ),
        omit_filter=omit_filter,
    )

    assert result.jobs[0]["command"] == "python train.py"
    assert result.jobs[0]["metadata"] == "{}"
    assert result.jobs[1]["metadata"] == {"safe": True}
