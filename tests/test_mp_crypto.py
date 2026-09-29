import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app import mp_crypto
from app.models import Tenant

TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture
def fernet_key(monkeypatch):
    """Configura una clave Fernet válida para el test."""
    monkeypatch.setattr(mp_crypto.settings, "MP_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    return TEST_KEY


def test_encrypt_decrypt_roundtrip(fernet_key):
    """El token cifrado se descifra devuelta al valor original."""
    original = "APP_USR-7134291311234567-093012-abc123def456_test"
    encrypted = mp_crypto.encrypt_token(original)
    assert encrypted != original
    assert original not in encrypted
    assert mp_crypto.decrypt_token(encrypted) == original


def test_ciphertext_is_opaque(fernet_key):
    """Dos cifrados del mismo valor producen ciphertext distinto (Fernet
    incluye IV random) — no hay reuso de nonce."""
    text_ = "TEST-123456"
    assert mp_crypto.encrypt_token(text_) != mp_crypto.encrypt_token(text_)


def test_decrypt_with_wrong_key_raises(monkeypatch):
    """Clave distinta no descifra: evita leer tokens con MP_TOKEN_KEY vieja."""
    monkeypatch.setattr(mp_crypto.settings, "MP_TOKEN_ENCRYPTION_KEY", TEST_KEY)
    encrypted = mp_crypto.encrypt_token("APP_USR-secret")
    monkeypatch.setattr(
        mp_crypto.settings,
        "MP_TOKEN_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )
    with pytest.raises(mp_crypto.MPTokenCryptoError):
        mp_crypto.decrypt_token(encrypted)


def test_missing_key_raises_clear_error(monkeypatch):
    """Sin MP_TOKEN_ENCRYPTION_KEY no se cifra nada: falla rápido y claro."""
    monkeypatch.setattr(mp_crypto.settings, "MP_TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(mp_crypto.MPTokenCryptoError, match="MP_TOKEN_ENCRYPTION_KEY"):
        mp_crypto.encrypt_token("x")


@pytest.mark.asyncio
async def test_tenant_stores_mp_tokens_encrypted(db_session, fernet_key):
    """Las columnas mp_* persisten ciphertext, no el token legible."""
    tenant = Tenant(
        name="Tenant MP",
        mp_user_id="123456789",
        mp_alias="negocio.demo",
        mp_public_key="APP_USR-pub-abc",
        mp_access_token_enc=mp_crypto.encrypt_token("APP_USR-access-secret"),
        mp_refresh_token_enc=mp_crypto.encrypt_token("TG-refresh-secret"),
    )
    db_session.add(tenant)
    await db_session.commit()

    raw = await db_session.execute(
        text(
            "SELECT mp_access_token_enc, mp_refresh_token_enc "
            "FROM tenant WHERE id = :tid"
        ).bindparams(tid=tenant.id)
    )
    row = raw.one()
    assert "access-secret" not in row.mp_access_token_enc
    assert "refresh-secret" not in row.mp_refresh_token_enc
    assert mp_crypto.decrypt_token(row.mp_access_token_enc) == "APP_USR-access-secret"
    assert mp_crypto.decrypt_token(row.mp_refresh_token_enc) == "TG-refresh-secret"
    # Campos no sensibles quedan legibles
    assert tenant.mp_alias == "negocio.demo"
    assert tenant.mp_public_key == "APP_USR-pub-abc"
