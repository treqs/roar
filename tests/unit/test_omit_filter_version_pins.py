"""A version pin is not a credential.

The env-var rule matched any name *containing* key/token/secret/..., case
insensitively, followed by "=". A pip requirement satisfies that: ``tiktoken==0.12.0``
is a name ending in "token" followed by "=", so the VERSION was redacted. A published
freeze then carried ``'tiktoken==[REDACTED]'``, which no installer can execute — the
recorded environment could not be rebuilt, which is the one thing a freeze exists to
make possible.

These pin both directions against the real patterns: package pins survive, and actual
environment assignments are still redacted.
"""

from __future__ import annotations

import pytest

from roar.filters.omit import OmitFilter

# Assembled at runtime so secret scanners do not flag the fixtures as real credentials.
FAKE_HF_TOKEN = "hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
FAKE_OPENAI_KEY = "sk-" + "AbCdEfGhIjKlMnOpQrStUv"


@pytest.fixture()
def omit_filter() -> OmitFilter:
    return OmitFilter({})


# Every one of these contains a keyword the rule looks for, and every one is a
# dependency people really install.
PACKAGE_PINS = [
    pytest.param("tiktoken==0.12.0", id="tiktoken"),
    pytest.param("authlib==1.3.2", id="authlib"),
    pytest.param("keyring==25.4.1", id="keyring"),
    pytest.param("tokenizers==0.20.3", id="tokenizers"),
    pytest.param("python-jose[cryptography]==3.3.0", id="python-jose"),
    pytest.param("secretstorage==3.3.3", id="secretstorage"),
]


@pytest.mark.parametrize("requirement", PACKAGE_PINS)
def test_package_pin_survives_filtering(omit_filter: OmitFilter, requirement: str) -> None:
    result = omit_filter.filter_string(requirement, field="packages")

    assert result.filtered == requirement
    assert result.detections == []


def test_full_install_command_survives(omit_filter: OmitFilter) -> None:
    # The shape that actually broke: a generated install line from a freeze. If any
    # version is replaced the command cannot be executed literally, which is exactly
    # the failure the reproducibility gate catches -- after the compute is spent.
    command = "pip install torch==2.9.1 tiktoken==0.12.0 regex==2025.9.1 authlib==1.3.2"

    result = omit_filter.filter_string(command, field="command")

    assert result.filtered == command
    assert "[REDACTED]" not in result.filtered


ENV_ASSIGNMENTS = [
    pytest.param(f"HF_TOKEN={FAKE_HF_TOKEN}", "HF_TOKEN", id="hf-token"),
    pytest.param(f"OPENAI_API_KEY={FAKE_OPENAI_KEY}", "OPENAI_API_KEY", id="openai-key"),
    pytest.param("MYTOKEN=abc123def456", "MYTOKEN", id="unprefixed-uppercase"),
    pytest.param("DB_PASSWORD=hunter2hunter2", "DB_PASSWORD", id="password"),
    pytest.param("AWS_SECRET=abcdefghijklmnop", "AWS_SECRET", id="secret"),
]


@pytest.mark.parametrize("assignment,name", ENV_ASSIGNMENTS)
def test_environment_assignment_is_still_redacted(
    omit_filter: OmitFilter, assignment: str, name: str
) -> None:
    # The reason the rule exists. Narrowing it must not cost this.
    result = omit_filter.filter_string(assignment, field="command")

    assert result.filtered == f"{name}=[REDACTED]"
    assert assignment.split("=", 1)[1] not in result.filtered


def test_assignment_inside_a_command_is_still_redacted(omit_filter: OmitFilter) -> None:
    command = f"env HF_TOKEN={FAKE_HF_TOKEN} python -m scripts.base_train --depth=14"

    result = omit_filter.filter_string(command, field="command")

    assert FAKE_HF_TOKEN not in result.filtered
    assert "HF_TOKEN=[REDACTED]" in result.filtered
    # The unrelated argument must survive intact.
    assert "--depth=14" in result.filtered


def test_provider_token_is_caught_by_value_even_when_the_name_is_lowercase(
    omit_filter: OmitFilter,
) -> None:
    # The gap the narrowing leaves is a lowercase variable name. Real providers are
    # still caught by the shape of the value, which is why that gap is acceptable.
    result = omit_filter.filter_string(f"hf_token={FAKE_HF_TOKEN}", field="command")

    assert FAKE_HF_TOKEN not in result.filtered
