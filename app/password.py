"""
Módulo de hashing y verificación de contraseñas de dueños de negocio.

Diseño:
- PBKDF2-HMAC-SHA256 con 600.000 iteraciones (recomendación actual OWASP).
- Sal criptográfica aleatoria de 16 bytes (128 bits) generada por secrets.token_hex.
- Comparación en tiempo constante usando hmac.compare_digest para prevenir
  ataques de temporización (timing attacks).
- IMPORTANTE: El costo computacional de 600k iteraciones está pensado para ejecutarse
  ÚNICAMENTE en login y registro (una sola vez por sesión). La autenticación por
  request en la API sigue utilizando API keys con SHA-256 (app/auth.py) y en el panel
  se utilizarán cookies de sesión firmadas. No llamar a verify_password por request.
"""

import hashlib
import hmac
import secrets

_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """
    Genera un hash seguro para la contraseña usando PBKDF2-HMAC-SHA256 con 600k iteraciones.
    Formato retornado: pbkdf2_sha256$<iteraciones>$<salt_hex>$<hash_hex>
    """
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _ITERATIONS,
    )
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${key.hex()}"


def verify_password(plain_password: str, password_hash: str) -> bool:
    """
    Verifica una contraseña en texto plano contra el hash guardado en tiempo constante.
    """
    if not password_hash or not plain_password:
        return False

    parts = password_hash.split("$")
    if len(parts) != 4:
        return False

    algorithm, iterations_str, salt, stored_hash = parts
    if algorithm != _ALGORITHM:
        return False

    try:
        iterations = int(iterations_str)
    except ValueError:
        return False

    computed_key = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )

    # Comparación segura a tiempo constante
    return hmac.compare_digest(computed_key.hex(), stored_hash)
