"""Tests for production env var validation in Settings."""

import pytest

from app.config import Settings

_PROD_BASE = {
    "ENVIRONMENT": "production",
    "SECRET_KEY": "a-valid-secret-key-that-is-not-default",
    "MP_SANDBOX": False,
    "META_APP_SECRET": "meta-secret",
    "MP_TOKEN_ENCRYPTION_KEY": "enc-key",
    "MP_SECRET_KEY": "mp-secret",
    "WHATSAPP_TOKEN": "wa-token",
    "WHATSAPP_PHONE_NUMBER_ID": "wa-phone-id",
    "MP_NOTIFICATION_URL": "https://api.example.test/webhooks/mercadopago",
}


def test_valid_production_settings():
    Settings(**_PROD_BASE)  # must not raise


@pytest.mark.parametrize(
    "missing_var",
    [
        "META_APP_SECRET",
        "MP_TOKEN_ENCRYPTION_KEY",
        "MP_SECRET_KEY",
        "WHATSAPP_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "MP_NOTIFICATION_URL",
    ],
)
def test_missing_critical_var_raises_in_production(missing_var):
    kwargs = {**_PROD_BASE, missing_var: ""}
    with pytest.raises(ValueError, match=missing_var):
        Settings(**kwargs)


def test_missing_multiple_vars_lists_all_in_error():
    kwargs = {**_PROD_BASE, "META_APP_SECRET": "", "WHATSAPP_TOKEN": ""}
    with pytest.raises(ValueError, match="META_APP_SECRET") as exc_info:
        Settings(**kwargs)
    assert "WHATSAPP_TOKEN" in str(exc_info.value)
