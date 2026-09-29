"""
Cifrado en reposo de las credenciales OAuth de Mercado Pago por tenant.

Los access_token y refresh_token de cada negocio son credenciales de cobro:
si un volcado de la base de datos se filtra, nadie debe poder leerlos.
Se usan tokens Fernet (cifrado simétrico autenticado, AES-128-CBC + HMAC)
con una clave única de la plataforma que vive solo en la env var
MP_TOKEN_ENCRYPTION_KEY (nunca en el código ni en la DB).

Generar la clave, una sola vez, con:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Regla: todo token de MP que se persista debe pasar por encrypt_token()
antes del INSERT/UPDATE, y por decrypt_token() para usarlo contra la API.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class MPTokenCryptoError(RuntimeError):
    """Falla de cifrado/descifrado de tokens OAuth de Mercado Pago."""


def _fernet() -> Fernet:
    key = settings.MP_TOKEN_ENCRYPTION_KEY
    if not key:
        raise MPTokenCryptoError(
            "MP_TOKEN_ENCRYPTION_KEY no está configurada; no se pueden "
            "cifrar ni usar tokens de Mercado Pago por tenant."
        )
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> str:
    """Cifra un token OAuth de MP para persistencia en reposo."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Descifra un token OAuth de MP leído de la DB."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise MPTokenCryptoError(
            "No se pudo descifrar el token: clave incorrecta o dato corrupto."
        ) from exc
