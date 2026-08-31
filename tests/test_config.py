from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from config import Settings


def test_valid_settings_load_successfully(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)

    assert settings.BOT_TOKEN == "123456:FAKE-TOKEN-FOR-TESTS"
    assert settings.admin_ids == [111111111, 222222222]
    assert settings.PAYMENT_ENABLED is False
    assert settings.ZONE_RADIUS_KM == 15


@pytest.mark.parametrize(
    "missing_key",
    [
        "BOT_TOKEN",
        "ADMIN_TELEGRAM_IDS",
        "REDIS_URL",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "SHEETS_MENU_ID",
        "SHEETS_ORDERS_ID",
        "GOOGLE_MAPS_API_KEY",
    ],
)
def test_missing_required_field_raises(valid_settings_kwargs, missing_key, monkeypatch):
    # Гарантируем отсутствие переменной в окружении процесса, иначе
    # BaseSettings может подхватить её из env вместо kwargs.
    monkeypatch.delenv(missing_key, raising=False)
    kwargs = dict(valid_settings_kwargs)
    del kwargs[missing_key]

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_admin_telegram_ids_rejects_non_numeric(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["ADMIN_TELEGRAM_IDS"] = "111,not-a-number"

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_admin_telegram_ids_rejects_empty(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["ADMIN_TELEGRAM_IDS"] = ""

    with pytest.raises(ValidationError):
        Settings(**kwargs)


@pytest.mark.parametrize("bad_url", ["localhost:6379", "http://localhost:6379", ""])
def test_redis_url_must_have_valid_scheme(valid_settings_kwargs, bad_url):
    kwargs = dict(valid_settings_kwargs)
    kwargs["REDIS_URL"] = bad_url

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_google_service_account_json_must_be_valid_base64(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["GOOGLE_SERVICE_ACCOUNT_JSON"] = "not-valid-base64!!!"

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_google_service_account_json_must_contain_required_keys(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    incomplete = base64.b64encode(b'{"client_email": "a@b.com"}').decode("ascii")
    kwargs["GOOGLE_SERVICE_ACCOUNT_JSON"] = incomplete

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_welcome_image_url_must_be_http_when_set(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["WELCOME_IMAGE_URL"] = "ftp://example.com/banner.png"

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_welcome_image_url_optional(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["WELCOME_IMAGE_URL"] = None

    settings = Settings(**kwargs)

    assert settings.WELCOME_IMAGE_URL is None


def test_unknown_timezone_rejected(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["TIMEZONE"] = "Mars/Olympus_Mons"

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_service_hours_order_must_be_open_lastorder_close(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["LAST_ORDER"] = "21:30"  # позже закрытия — некорректно

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_payment_enabled_requires_a_provider(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["PAYMENT_ENABLED"] = True
    kwargs["PAPARA_BUSINESS_ID"] = None
    kwargs["IYZICO_API_KEY"] = None

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_payment_enabled_passes_with_iyzico_key(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["PAYMENT_ENABLED"] = True
    kwargs["IYZICO_API_KEY"] = "fake-iyzico-key"

    settings = Settings(**kwargs)

    assert settings.PAYMENT_ENABLED is True


def test_service_hours_properties_parse_to_time(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)

    assert settings.service_open_time.hour == 10
    assert settings.last_order_time.minute == 15
    assert settings.service_close_time.hour == 21


def test_google_service_account_info_decodes_back_to_dict(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)

    info = settings.google_service_account_info

    assert info["client_email"] == "bot@antalya-bot.iam.gserviceaccount.com"
