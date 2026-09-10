"""A package version is not a credential; a credential is not a package version.

Both halves have been broken by a fix for the other. Redacting a version makes the
published freeze uninstallable, which cost a 3.5-hour training run its reproducibility
gate twice. Narrowing the name pattern to stop that stopped real credentials from being
redacted -- first every lowercase name, then 42 mixed-case ones, then package names
containing a delimited keyword (google-auth) in the serialized form.

So each case below is asserted in every serialization roar actually writes: the
``name==version`` string, the ``{"name": "version"}`` JSON the record serializes, and
the escaped JSON nested inside a serialized command.
"""

from __future__ import annotations

import json

import pytest

from roar.filters.omit import OmitFilter

# Assembled at runtime so secret scanners do not flag the fixtures.
HF = "hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
SK = "sk-" + "AbCdEfGhIjKlMnOpQrStUv"
OPAQUE = "hunter2hunter2"  # a real secret with no recognisable shape


@pytest.fixture()
def f() -> OmitFilter:
    return OmitFilter({})


def forms(name: str, value: str) -> list[str]:
    """The name/value pair in every serialization the record is written in."""
    return [
        f"{name}=={value}",
        f"{name}={value}",
        json.dumps({name: value}),
        json.dumps(json.dumps({name: value})),  # escaped, nested in a command string
    ]


# Real dependencies whose names carry a keyword. None is a credential.
# google-auth and friends carry a *delimited* keyword, so only the version guard
# spares them -- they are the case a name-only pattern cannot get right.
PACKAGES = [
    "tiktoken", "authlib", "keyring", "tokenizers", "secretstorage",
    "python-jose", "google-auth", "google-auth-oauthlib", "dj-rest-auth",
    "social-auth-core", "oauthlib", "azure-keyvault",
]

# Names that denote a credential, across every casing convention in use. Each was
# redacted by 0.4.5 and silently stopped being redacted by one of its successors.
SECRET_NAMES = [
    "HF_TOKEN", "hf_token", "Hf_Token", "HfToken", "MYTOKEN", "MyToken", "myToken",
    "API_KEY", "api_key", "api-key", "Api_Key", "apiKey", "ApiKey", "APIKey",
    "secretValue", "SecretValue", "TOKEN_my", "Github_Token", "X-Api-Key",
    "Token", "Key", "Secret", "Password", "Credential", "DB_PASSWORD",
]


@pytest.mark.parametrize("package", PACKAGES)
@pytest.mark.parametrize("version", ["0.11.0", "2.0.0rc1", "1.0.dev4", "2.9.1+cu121"])
def test_a_version_pin_survives_every_serialization(f: OmitFilter, package, version):
    for text in forms(package, version):
        assert f.filter_string(text, field="packages").filtered == text, text


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_a_credential_is_redacted_in_every_serialization(f: OmitFilter, name):
    # Narrowing the name pattern must never cost this. Every casing here is a name a
    # real project uses for a real secret.
    for text in forms(name, OPAQUE):
        if text.startswith(f"{name}=="):
            continue  # "==" is the version-pin form, deliberately exempt
        assert OPAQUE not in f.filter_string(text, field="runtime").filtered, text


def test_a_keyword_inside_a_word_is_not_a_credential_name(f: OmitFilter):
    # "tokenizer" contains "token" but is one word; "api_key" carries it as its own.
    # This boundary is what keeps a recorded command executable.
    command = "python -m train --tokenizer=gpt2 --depth=14 --lr=3e-4"
    assert f.filter_string(command, field="command").filtered == command

    install = "pip install torch==2.9.1 tiktoken==0.11.0 google-auth==2.35.0"
    assert f.filter_string(install, field="command").filtered == install


def test_a_secret_is_caught_by_value_even_when_the_name_says_nothing(f: OmitFilter):
    # The backstop for names no pattern anticipates.
    assert HF not in f.filter_string(f"whatever={HF}", field="command").filtered
    assert SK not in f.filter_string(f'{{"opts": "{SK}"}}', field="runtime").filtered


def test_a_real_record_keeps_its_versions_and_loses_its_secrets(f: OmitFilter):
    """Both halves of one record, through the entry point that publishes it.

    A filter that stopped redacting would pass every "version survives" case above and
    be catastrophically wrong, so the two are asserted together. Driven through
    filter_metadata rather than filter_string because a fix verified only on the string
    form is exactly how the broken version shipped.
    """
    packages = {p: "1.2.3" for p in PACKAGES}
    record = {
        "packages": {"pip": packages},
        # the same map as a serialized blob -- the form that reached a published freeze
        # as {"tiktoken": "[REDACTED]"} when only the string form had been fixed
        "python_capture": json.dumps({"pip": packages}),
        "runtime": {
            "env_vars": {"HF_TOKEN": HF, "api_key": SK, "PATH": "/usr/local/bin"},
            "command": f"env HF_TOKEN={HF} python -m train --tokenizer=gpt2 --depth=14",
        },
        "git": {"remote_url": f"https://x-access-token:{HF}@github.com/org/repo.git"},
    }

    out, _ = f.filter_metadata(json.loads(json.dumps(record)))
    blob = json.dumps(out)

    # Every version intact, in both serializations -- the freeze must stay installable.
    assert out["packages"]["pip"] == packages
    assert json.loads(out["python_capture"])["pip"] == packages

    # Every credential gone, by name and by value.
    for secret in (HF, SK):
        assert secret not in blob
    for name in ("HF_TOKEN", "api_key"):
        # marker varies: a value-shaped rule may claim it first ([HF_TOKEN_REDACTED])
        assert "REDACTED" in out["runtime"]["env_vars"][name], name

    # Innocent environment survives, including unrelated "=" in the command.
    assert out["runtime"]["env_vars"]["PATH"] == "/usr/local/bin"
    assert "--depth=14" in out["runtime"]["command"]
    assert "--tokenizer=gpt2" in out["runtime"]["command"]
