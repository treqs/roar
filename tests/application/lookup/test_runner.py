from roar.application.lookup.models import LookupSource
from roar.application.lookup.runner import run_local_then_remote_lookup


def test_run_local_then_remote_lookup_prefers_local_and_skips_remote() -> None:
    remote_called = False

    def _remote():
        nonlocal remote_called
        remote_called = True
        return {"id": "remote"}, None

    result = run_local_then_remote_lookup(
        lookup_local=lambda: {"id": "local"},
        lookup_remote=_remote,
        allow_remote=True,
    )

    assert result.value == {"id": "local"}
    assert result.source == LookupSource.LOCAL
    assert result.error is None
    assert remote_called is False


def test_run_local_then_remote_lookup_returns_remote_value_on_local_miss() -> None:
    result = run_local_then_remote_lookup(
        lookup_local=lambda: None,
        lookup_remote=lambda: ({"id": "remote"}, None),
        allow_remote=True,
    )

    assert result.value == {"id": "remote"}
    assert result.source == LookupSource.REMOTE
    assert result.error is None


def test_run_local_then_remote_lookup_returns_remote_error() -> None:
    result = run_local_then_remote_lookup(
        lookup_local=lambda: None,
        lookup_remote=lambda: (None, "permission denied"),
        allow_remote=True,
    )

    assert result.value is None
    assert result.source == LookupSource.REMOTE
    assert result.error == "permission denied"


def test_run_local_then_remote_lookup_returns_none_when_remote_disabled() -> None:
    result = run_local_then_remote_lookup(
        lookup_local=lambda: None,
        lookup_remote=lambda: ({"id": "remote"}, None),
        allow_remote=False,
    )

    assert result.value is None
    assert result.source == LookupSource.NONE
    assert result.error is None
