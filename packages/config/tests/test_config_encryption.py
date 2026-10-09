"""Encrypt--/Encrypted-- handling and Fernet key resolution in create_app_settings."""
import logging
import re

import pytest
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.config import create_app_settings, destroy_settings, get_settings

PLAINTEXT = "s3cret-value"


class SecretConfig(BaseSettings):
    api_key: str = "plain"
    retries: int = 3
    model_config = SettingsConfigDict(env_prefix="SECRETCFG_")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    destroy_settings()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SECRETCFG_API_KEY", raising=False)
    yield
    destroy_settings()


def _encrypt(key: bytes, value: str = PLAINTEXT) -> str:
    return Fernet(key).encrypt(value.encode()).decode()


def _create(caplog):
    with caplog.at_level(logging.INFO, logger="config.config"):
        return create_app_settings("secret-app", SecretConfig, env_file="")


def test_decrypts_with_private_key_env_and_redacts_log(monkeypatch, caplog):
    key = Fernet.generate_key()
    monkeypatch.setenv("PRIVATE_KEY", key.decode())
    monkeypatch.setenv("SECRETCFG_API_KEY", f"Encrypted--{_encrypt(key)}")

    settings, provider = _create(caplog)

    assert settings.api_key == PLAINTEXT
    assert provider()["api_key"] == PLAINTEXT
    assert PLAINTEXT not in caplog.text
    assert '"api_key": "****"' in caplog.text
    assert '"retries": 3' in caplog.text


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_encrypted_value_keeps_its_quotes(monkeypatch, caplog, quote):
    key = Fernet.generate_key()
    monkeypatch.setenv("PRIVATE_KEY", key.decode())
    monkeypatch.setenv("SECRETCFG_API_KEY", f"{quote}Encrypted--{_encrypt(key)}{quote}")

    settings, _ = _create(caplog)
    assert settings.api_key == f"{quote}{PLAINTEXT}{quote}"


def test_key_read_from_private_key_file(tmp_path, monkeypatch, caplog):
    key = Fernet.generate_key()
    (tmp_path / ".private.key").write_text(key.decode() + "\n")
    monkeypatch.setenv("SECRETCFG_API_KEY", f"Encrypted--{_encrypt(key)}")

    settings, _ = _create(caplog)
    assert settings.api_key == PLAINTEXT


def test_env_key_wins_over_private_key_file(tmp_path, monkeypatch, caplog):
    file_key, env_key = Fernet.generate_key(), Fernet.generate_key()
    (tmp_path / ".private.key").write_text(file_key.decode())
    monkeypatch.setenv("PRIVATE_KEY", env_key.decode())
    monkeypatch.setenv("SECRETCFG_API_KEY", f"Encrypted--{_encrypt(env_key)}")

    settings, _ = _create(caplog)
    assert settings.api_key == PLAINTEXT


def test_private_key_directory_is_ignored_with_warning(tmp_path, monkeypatch, caplog):
    (tmp_path / ".private.key").mkdir()
    monkeypatch.setenv("SECRETCFG_API_KEY", "Encrypted--whatever")

    with pytest.raises(RuntimeError, match="no Fernet key available"):
        _create(caplog)
    assert "found a directory" in caplog.text


def test_encrypted_value_without_any_key_fails_and_caches_nothing(monkeypatch, caplog):
    monkeypatch.setenv("SECRETCFG_API_KEY", "Encrypted--whatever")
    with pytest.raises(RuntimeError, match="api_key"):
        _create(caplog)
    with pytest.raises(RuntimeError, match="has not been set"):
        get_settings()


def test_undecryptable_value_is_left_as_is_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("PRIVATE_KEY", Fernet.generate_key().decode())
    foreign = f"Encrypted--{_encrypt(Fernet.generate_key())}"
    monkeypatch.setenv("SECRETCFG_API_KEY", foreign)

    settings, _ = _create(caplog)

    assert settings.api_key == foreign
    assert "Failed to decrypt field 'api_key'" in caplog.text


def _logged_ciphertext(caplog) -> str:
    match = re.search(r"encrypted as: [\"']?Encrypted--([A-Za-z0-9_\-=]+)", caplog.text)
    assert match, caplog.text
    return match.group(1)


def test_encrypt_marker_generates_key_file_and_refuses_to_start(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SECRETCFG_API_KEY", f"Encrypt--{PLAINTEXT}")

    with pytest.raises(RuntimeError, match="need to be encrypted"):
        _create(caplog)

    key_file = tmp_path / ".private.key"
    assert key_file.is_file()
    ciphertext = _logged_ciphertext(caplog)
    assert Fernet(key_file.read_text().encode()).decrypt(ciphertext.encode()).decode() == PLAINTEXT


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_encrypt_marker_uses_existing_env_key(tmp_path, monkeypatch, caplog, quote):
    key = Fernet.generate_key()
    monkeypatch.setenv("PRIVATE_KEY", key.decode())
    monkeypatch.setenv("SECRETCFG_API_KEY", f"{quote}Encrypt--{PLAINTEXT}{quote}")

    with pytest.raises(RuntimeError, match="need to be encrypted"):
        _create(caplog)

    assert not (tmp_path / ".private.key").exists()
    assert f"encrypted as: {quote}Encrypted--" in caplog.text
    assert Fernet(key).decrypt(_logged_ciphertext(caplog).encode()).decode() == PLAINTEXT
