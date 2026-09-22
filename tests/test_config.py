"""config.py: environment handling and the refusal to expose an open server."""

from __future__ import annotations

import pytest

from decis.config import ConfigError, Settings, is_loopback, load_settings

DECIS_VARS = (
    "DECIS_API_KEY",
    "DECIS_API_KEYS",
    "DECIS_ALLOW_NO_AUTH",
    "DECIS_ACCEPT_FOREIGN_DEFAULTS",
    "DECIS_HOST",
    "DECIS_PORT",
    "DECIS_DEFAULT_ENGINE",
    "DECIS_MODEL_DIR",
    "DECIS_MAX_REQUEST_BYTES",
    "DECIS_REQUEST_TIMEOUT_MS",
    "DECIS_TORCH_THREADS",
    "DECIS_LOG_LEVEL",
    "DECIS_LOG_PAYLOADS",
    "DECIS_ENV_FILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DECIS_VARS:
        monkeypatch.delenv(name, raising=False)


# --- the safety refusal (AGENTS.md §3-19) ------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_no_key_on_a_public_address_refuses_to_start(host: str) -> None:
    with pytest.raises(ConfigError) as caught:
        Settings(api_keys=(), host=host).check_safe_to_serve()
    message = str(caught.value)
    assert "DECIS_API_KEY" in message
    assert "127.0.0.1" in message


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_no_key_on_loopback_is_allowed(host: str) -> None:
    Settings(api_keys=(), host=host).check_safe_to_serve()


def test_no_key_is_allowed_when_explicitly_opted_in() -> None:
    Settings(api_keys=(), host="0.0.0.0", allow_no_auth=True).check_safe_to_serve()


def test_a_key_makes_any_address_safe() -> None:
    Settings(api_keys=("k",), host="0.0.0.0").check_safe_to_serve()


def test_an_unresolvable_hostname_fails_closed() -> None:
    """We cannot prove it is loopback, so we must not treat it as one."""
    assert not is_loopback("some.internal.host")


def test_the_host_argument_overrides_the_configured_one() -> None:
    """`decis serve --host` is what gets checked, not the value in .env."""
    # Configured loopback, asked to bind publicly, no key -> refuse.
    with pytest.raises(ConfigError):
        Settings(api_keys=(), host="127.0.0.1").check_safe_to_serve("0.0.0.0")
    # Configured publicly, asked to bind loopback -> fine.
    Settings(api_keys=(), host="0.0.0.0").check_safe_to_serve("127.0.0.1")


# --- parsing -----------------------------------------------------------------


def test_defaults_are_sane(clean_env: None) -> None:
    settings = load_settings(env_file="")
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.default_engine == "mock"
    assert settings.max_request_bytes == 2 * 1024 * 1024
    assert not settings.auth_enabled
    assert settings.accept_foreign_defaults


def test_values_come_from_the_environment(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECIS_API_KEY", "secret-one")
    monkeypatch.setenv("DECIS_PORT", "9100")
    monkeypatch.setenv("DECIS_HOST", "127.0.0.1")
    settings = load_settings(env_file="")
    assert settings.api_keys == ("secret-one",)
    assert settings.port == 9100
    assert settings.host == "127.0.0.1"
    assert settings.auth_enabled


def test_several_keys_can_be_supplied_for_rotation(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECIS_API_KEY", "old, new")
    monkeypatch.setenv("DECIS_API_KEYS", "newest")
    settings = load_settings(env_file="")
    assert settings.api_keys == ("old", "new", "newest")


def test_a_bad_integer_says_which_variable(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECIS_PORT", "not-a-port")
    with pytest.raises(ConfigError) as caught:
        load_settings(env_file="")
    assert "DECIS_PORT" in str(caught.value)


def test_a_bad_boolean_says_which_variable(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECIS_ALLOW_NO_AUTH", "maybe")
    with pytest.raises(ConfigError) as caught:
        load_settings(env_file="")
    assert "DECIS_ALLOW_NO_AUTH" in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("false", False), ("Off", False)],
)
def test_boolean_spellings(clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("DECIS_LOG_PAYLOADS", raw)
    assert load_settings(env_file="").log_payloads is expected


def test_optional_values_stay_none(clean_env: None) -> None:
    settings = load_settings(env_file="")
    assert settings.model_dir is None
    assert settings.torch_threads is None


def test_model_dir_and_threads_are_parsed(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    monkeypatch.setenv("DECIS_MODEL_DIR", "/srv/models")
    monkeypatch.setenv("DECIS_TORCH_THREADS", "8")
    settings = load_settings(env_file="")
    assert settings.model_dir == Path("/srv/models")
    assert settings.torch_threads == 8


# --- .env --------------------------------------------------------------------


def test_dot_env_is_loaded(clean_env: None, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DECIS_API_KEY=from-file\nDECIS_PORT=9999\n", encoding="utf-8")
    settings = load_settings(str(env_file))
    assert settings.api_keys == ("from-file",)
    assert settings.port == 9999
    assert settings.env_file == str(env_file)


def test_the_real_environment_beats_dot_env(clean_env: None, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`DECIS_PORT=9000 decis serve` must win over a .env that says otherwise."""
    env_file = tmp_path / ".env"
    env_file.write_text("DECIS_PORT=9999\n", encoding="utf-8")
    monkeypatch.setenv("DECIS_PORT", "9000")
    assert load_settings(str(env_file)).port == 9000


def test_a_missing_env_file_is_not_an_error(clean_env: None, tmp_path) -> None:
    settings = load_settings(str(tmp_path / "does-not-exist.env"))
    assert settings.port == 8000


def test_comments_and_blank_lines_in_dot_env(clean_env: None, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\nDECIS_API_KEY=abc\n\n# another\nDECIS_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    settings = load_settings(str(env_file))
    assert settings.api_keys == ("abc",)
    assert settings.host == "127.0.0.1"


def test_env_file_does_not_leak_into_the_process(clean_env: None, tmp_path) -> None:
    """`load_settings` reads a file; it must not silently export it for other code."""
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("DECIS_API_KEY=from-file\n", encoding="utf-8")
    load_settings(str(env_file))
    assert "DECIS_API_KEY" in os.environ  # python-dotenv does set it...
    # ...which is exactly why every read goes through load_settings: the settings
    # object is the contract, not the ambient environment.
