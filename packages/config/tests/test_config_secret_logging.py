"""config must not print secrets at startup.

Two leaks:

1. When an `Encrypt--` value was present and no key file existed, config
   generated a Fernet key and logged THE KEY ITSELF at WARNING. That is the
   master key for every `Encrypted--` value in the app — in the log stream,
   which is stdout / `docker logs` / the log_event table. Anyone who can read
   a log could decrypt every encrypted secret. Only the path should be logged.

2. The "Creating Settings" dump masked by encryption PROVENANCE only: a field
   was hidden if its .env value arrived as `Encrypted--`. Provenance is not a
   security property. Any credential left in plaintext in .env — the normal
   state locally and on a fresh deployment — was printed in full at INFO on
   every boot.
"""
import logging

import pytest
from cryptography.fernet import Fernet

from config.config import (
    _is_secret_field_name,
    _loggable_setting_value,
)


# --------------------------------------------------------------------------
# 2. The startup settings dump
# --------------------------------------------------------------------------

# Field names shaped like real app Config models.
SECRET_FIELDS = [
    "postgres_password",
    "smtp_password",
    "smtp_key",
    "llm_api_key",
    "slack_token",
    "twilio_auth_token",
    "ocr_credentials_file_key",
    "jwt_secret",
    "session_secret",
]

# Field names that must stay fully visible — masking these would break the
# debuggability the dump exists for.
NON_SECRET_FIELDS = [
    "postgres_host",
    "postgres_port",
    "postgres_user",
    "postgres_database",
    "smtp_user",
    "smtp_from",
    "auth_type",              # a Literal, not a credential
    "ocr_credentials_file",   # a PATH — worth seeing
    "llm_model",
    "llm_provider",
    "mock_vendor_base_url",
    "twilio_account_sid",
    "log_format",
]


@pytest.mark.parametrize("name", SECRET_FIELDS)
def test_secret_field_is_masked_even_when_plaintext(name):
    """The leak: value never went through Encrypt--, so provenance masking
    left it in the clear."""
    out = _loggable_setting_value(name, "live-secret-value", was_encrypted=False)
    assert out == "****"


@pytest.mark.parametrize("name", NON_SECRET_FIELDS)
def test_non_secret_field_is_untouched(name):
    assert _loggable_setting_value(name, "visible-value", was_encrypted=False) == "visible-value"
    assert _loggable_setting_value(name, 5432, was_encrypted=False) == 5432


@pytest.mark.parametrize("name", SECRET_FIELDS)
@pytest.mark.parametrize("empty", [None, ""])
def test_unset_secret_stays_visible_as_unset(name, empty):
    """Debuggability: an operator must still be able to see that a credential
    is MISSING — that is the usual startup question — without seeing any set
    value."""
    assert _loggable_setting_value(name, empty, was_encrypted=False) == empty


@pytest.mark.parametrize("name", SECRET_FIELDS + NON_SECRET_FIELDS)
def test_encrypted_provenance_still_masks(name):
    """Pre-existing behavior must not regress."""
    assert _loggable_setting_value(name, "anything", was_encrypted=True) == "****"


@pytest.mark.parametrize("name", SECRET_FIELDS)
def test_is_secret_field_name_positive(name):
    assert _is_secret_field_name(name) is True


@pytest.mark.parametrize("name", NON_SECRET_FIELDS)
def test_is_secret_field_name_negative(name):
    assert _is_secret_field_name(name) is False


# --------------------------------------------------------------------------
# 1. The generated Fernet key
# --------------------------------------------------------------------------

def test_generated_fernet_key_is_never_logged(tmp_path, monkeypatch, caplog):
    """Drive the real generate-a-key branch and assert the key does not reach
    the log. Uses a throwaway key in a tmp dir — no real credential involved."""
    from pydantic_settings import BaseSettings
    from pydantic import ConfigDict

    class _Cfg(BaseSettings):
        some_secret: str = ""
        model_config = ConfigDict(env_prefix="LEAKTEST_", env_file=".env")

    env = tmp_path / ".env"
    env.write_text("LEAKTEST_SOME_SECRET=Encrypt--please-encrypt-me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    import config.config as config_mod
    config_mod._settings_cache.clear()

    with caplog.at_level(logging.DEBUG):
        # The Encrypt-- flow always ends by telling the operator to update
        # .env and restart; we only care about what it logged on the way.
        with pytest.raises(RuntimeError):
            config_mod.create_app_settings("leaktest", _Cfg, env_file=str(env))

    key_file = tmp_path / ".private.key"
    assert key_file.exists(), "the generate-a-key branch did not run"
    generated = key_file.read_text(encoding="utf-8").strip()
    Fernet(generated.encode())  # sanity: it really is a Fernet key

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert generated not in emitted, "the Fernet master key reached the log"
    # The path is still logged — that is what the operator needs.
    assert ".private.key" in emitted

    config_mod._settings_cache.clear()
