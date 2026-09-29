import pytest
from app.phone import InvalidPhoneError, normalize_whatsapp_phone


# ---------------------------------------------------------------------------
# Formatos válidos (como los escribe la gente real)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Característica + número, como lo escribe la mayoría
        ("3584166288", "5493584166288"),
        ("11 5555 5555", "5491155555555"),
        ("11 55555555", "5491155555555"),
        # Con 0 inicial
        ("03584166288", "5493584166288"),
        ("011 5555 5555", "5491155555555"),
        # Notación vieja con 15
        ("3584 15 166288", "5493584166288"),
        ("11 15 5555 5555", "5491155555555"),
        ("03584 15 166288", "5493584166288"),
        # Con código de país
        ("+54 9 3584 166288", "5493584166288"),
        ("+54 9 11 5555 5555", "5491155555555"),
        ("+54 3584 166288", "5493584166288"),
        ("+54 0 3584 166288", "5493584166288"),
        ("54 9 3584166288", "5493584166288"),
        ("5493584166288", "5493584166288"),
        # Ya normalizado pero con un 0 extra tras el 9
        ("+54 9 0 3584 166288", "5493584166288"),
        # Prefijo internacional 00
        ("00 54 9 3584 166288", "5493584166288"),
    ],
)
def test_normalize_valid_formats(raw, expected):
    assert normalize_whatsapp_phone(raw) == expected


# ---------------------------------------------------------------------------
# Formatos inválidos: deben rechazarse antes de crear la reserva
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "358416628",  # un dígito menos
        "1234",
        "abcdefgh",
        "54 9 35841",  # número nacional incompleto con código de país
        "+54 9",  # solo el prefijo
        "111",
        None,
    ],
)
def test_normalize_rejects_invalid(raw):
    with pytest.raises(InvalidPhoneError):
        normalize_whatsapp_phone(raw)
