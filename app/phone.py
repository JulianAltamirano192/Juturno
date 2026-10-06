# app/phone.py
"""Normalización de teléfonos de Argentina para envío por WhatsApp.

Meta/WhatsApp exige el número completo de móvil argentino:
'549' + característica + número (solo dígitos, sin '+'). Los clientes
escriben su teléfono de muchas maneras (con o sin 0, con o sin el 9,
con la notación vieja del 15), así que se normaliza antes de guardar
la reserva; si el formato no es reconocible se rechaza el input para
que el usuario lo corriente en pantalla antes de pagar la seña: una
reserva confirmada con un teléfono no entregable es una reserva cuyo
WhatsApp nunca va a llegar.
"""

import re


class InvalidPhoneError(ValueError):
    """El teléfono no puede interpretarse como móvil argentino válido."""


# Número nacional de móvil: característica (2 a 4 dígitos) + número (6 a 8)
_NATIONAL_LENGTH = 10


def normalize_whatsapp_phone(raw: str) -> str:
    """
    Normaliza un teléfono argentino al formato '549XXXXXXXXXX'.

    Acepta (con espacios, guiones o paréntesis de por medio):
      - '3584166288'            característica + número
      - '03584 166288'          con 0 inicial
      - '3584 15 166288'        notación vieja con 15
      - '+54 9 3584 166288'     código de país completo
      - '+54 3584 166288'       código de país sin el 9
      - '5493584166288'         ya normalizado

    Lanza InvalidPhoneError si el formato no es reconocible.
    """
    if not isinstance(raw, str):
        raise InvalidPhoneError("El teléfono debe ser un texto")

    # Solo dígitos; los ceros iniciales son prefijos de marcación
    digits = re.sub(r"\D", "", raw).lstrip("0")
    if not digits:
        raise InvalidPhoneError("El teléfono está vacío")

    # Con código de país: '54' + (opcional '9') + número nacional
    if digits.startswith("54"):
        national = digits[2:]
        national = national.removeprefix("0")
        if national.startswith("9"):
            # ya trae el 9 de móvil
            number = national[1:]
            number = number.removeprefix("0")
            if len(number) == _NATIONAL_LENGTH:
                return "549" + number
        elif len(national) == _NATIONAL_LENGTH:
            # 54 sin el 9 de móvil
            return "549" + national
        raise InvalidPhoneError("Número incompleto tras el código de país")

    # Notación vieja con 15: característica (2 a 4 dígitos) + '15' + número
    if len(digits) == _NATIONAL_LENGTH + 2:
        for area_len in (2, 3, 4):
            if digits[area_len : area_len + 2] == "15":
                digits = digits[:area_len] + digits[area_len + 2 :]
                break

    if len(digits) == _NATIONAL_LENGTH:
        return "549" + digits

    raise InvalidPhoneError("Formato de teléfono no reconocido")
