"""A version pin is not a credential, in any serialization.

The env-var rule matched any name *containing* key/token/secret/..., case
insensitively, followed by "=". A pip requirement satisfies that: ``tiktoken==0.12.0``
is a name ending in "token" followed by "=", so the VERSION was redacted. A published
freeze then carried ``'tiktoken==[REDACTED]'``, which no installer can execute — the
recorded environment could not be rebuilt, which is the one thing a freeze exists to
make possible.

0.4.6 fixed the ``name==version`` string form and shipped. It did not fix
``{"name": "version"}`` -- and ``roar`` records packages as ``dict[str, str]`` keyed by
package name, so the published freeze went on carrying ``"tiktoken": "[REDACTED]"``.
The on-host checks all passed, because the installed distribution and the string form
were both genuinely fine; only the serialized record was wrong. A second 3.5-hour run
was spent discovering that.

So these pin both directions in BOTH serializations: the string form, the JSON form,
and the full record shape as it is actually written.
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


# The shape roar actually serializes: dict[str, str] keyed by package name.
# See roar/core/models/provenance.py -- used_packages, installed_packages, packages.
def test_serialized_package_map_survives(omit_filter: OmitFilter) -> None:
    import json

    packages = {
        "requests": "2.34.2",
        "tiktoken": "0.11.0",
        "authlib": "1.3.2",
        "keyring": "25.4.1",
        "tokenizers": "0.20.3",
        "secretstorage": "3.3.3",
        "torch": "2.9.1",
    }
    blob = json.dumps({"pip": packages, "used_packages": packages})

    result = omit_filter.filter_string(blob, field="freeze")

    assert "[REDACTED]" not in result.filtered
    assert json.loads(result.filtered)["pip"] == packages


JSON_SECRETS = [
    pytest.param(f'{{"HF_TOKEN": "{FAKE_HF_TOKEN}"}}', id="uppercase-env"),
    pytest.param(f'{{"api_key": "{FAKE_OPENAI_KEY}"}}', id="delimited-lowercase"),
    pytest.param(f'{{"accessToken": "{FAKE_HF_TOKEN}"}}', id="camelcase"),
    pytest.param('{"MYTOKEN": "abc123def456ghi"}', id="unprefixed-uppercase"),
    pytest.param('{"password": "hunter2hunter2"}', id="bare-keyword"),
]


@pytest.mark.parametrize("blob", JSON_SECRETS)
def test_json_named_secret_is_still_redacted(omit_filter: OmitFilter, blob: str) -> None:
    # Narrowing the name pattern must not cost the case the rule exists for. A
    # credential in a serialized environment is the thing being protected.
    result = omit_filter.filter_string(blob, field="runtime")

    assert "[REDACTED]" in result.filtered
    assert blob.rsplit('": "', 1)[1].rstrip('"}') not in result.filtered


def test_lowercase_delimited_assignment_is_redacted(omit_filter: OmitFilter) -> None:
    # Strictly better than 0.4.6, which dropped IGNORECASE wholesale and so stopped
    # matching lowercase names entirely. A delimiter distinguishes a credential name
    # from a package name without giving up on lowercase.
    result = omit_filter.filter_string(f"api_key={FAKE_OPENAI_KEY}", field="command")

    assert result.filtered == "api_key=[REDACTED]"


def test_a_real_record_keeps_its_versions_and_loses_its_secrets(omit_filter: OmitFilter) -> None:
    """The true positive and the false positive in one artifact.

    A filter that stopped redacting would pass every "package survives" case above
    and be catastrophically wrong. This asserts both halves of the same record: the
    dependency versions come through intact, and a credential sitting beside them in
    the captured environment does not.
    """
    import json

    record = {
        "pip": {
            "requests": "2.34.2",
            "tiktoken": "0.11.0",
            "authlib": "1.3.2",
            "keyring": "25.4.1",
            "tokenizers": "0.20.3",
            "secretstorage": "3.3.3",
            "torch": "2.9.1",
        },
        "runtime": {
            "env_vars": {
                "HF_TOKEN": FAKE_HF_TOKEN,
                "OPENAI_API_KEY": FAKE_OPENAI_KEY,
                "api_key": "sk-" + "lowercaseDelimited123",
                "accessToken": "camel" + "CaseSecret456",
                "PATH": "/usr/local/bin:/usr/bin",
            },
            "command": f"env HF_TOKEN={FAKE_HF_TOKEN} python -m scripts.train --depth=14",
        },
    }

    result = omit_filter.filter_string(json.dumps(record), field="freeze")
    out = json.loads(result.filtered)

    # Every version intact -- the freeze must remain installable.
    assert out["pip"] == record["pip"]

    # Every credential gone, by name shape and by value.
    for name in ("HF_TOKEN", "OPENAI_API_KEY", "api_key", "accessToken"):
        assert out["runtime"]["env_vars"][name] == "[REDACTED]", name
    for secret in (FAKE_HF_TOKEN, FAKE_OPENAI_KEY):
        assert secret not in result.filtered

    # Innocent environment survives, including an unrelated "=" in the command.
    assert out["runtime"]["env_vars"]["PATH"] == "/usr/local/bin:/usr/bin"
    assert "--depth=14" in out["runtime"]["command"]
    assert "HF_TOKEN=[REDACTED]" in out["runtime"]["command"]
