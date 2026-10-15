import re
import unicodedata
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.models import (
    BUSINESS_CATEGORIES,
    BUSINESS_CATEGORY_OTHER,
    BUSINESS_CATEGORY_OTHER_MAX,
)

CLIENT_NAME_MAX = 60
# Links y dominios (spam.com, www.x, https://...): el nombre llega al
# WhatsApp que manda el negocio. TLDs explícitos para no rechazar "J.Perez".
# ponytail: lista fija de TLDs comunes; sumar si aparece spam con otro.
_LINK = re.compile(
    r"://|www[.。]|[\w-]+[.。](?:com|net|org|ar|io|ly|me|co|xyz|app|site|link"
    r"|info|online|shop|store|biz|gl|gg|to|cc|tk|ru|br|es|us|uk|mx|cl|uy|pe)(?!\w)",
    re.IGNORECASE,
)


def clean_client_name(raw: str) -> str:
    """Nombre del cliente normalizado (NFKC, espacios colapsados, sin saltos
    de línea, que Meta rechaza en parámetros de template). ValueError si es
    muy corto/largo, trae caracteres invisibles o de control, o un link."""
    name = " ".join(unicodedata.normalize("NFKC", raw).split())
    # El largo va primero: la regex es cuadrática con textos largos.
    if not 2 <= len(name) <= CLIENT_NAME_MAX:
        raise ValueError(name)
    if any(unicodedata.category(c)[0] == "C" for c in name):
        raise ValueError(name)  # NUL, zero-width, bidi
    if not any(c.isalpha() for c in name) or _LINK.search(name):
        raise ValueError(name)
    return name


def clean_business_category(
    category: str | None, other: str | None
) -> tuple[str, str | None]:
    """Rubro elegido → (clave, texto de "Otro" o None).

    ValueError("category") si la clave no está en la lista; ValueError("other")
    si eligió "Otro" sin un texto de 2 a 60 caracteres sin caracteres de control.
    """
    if category not in BUSINESS_CATEGORIES:
        raise ValueError("category")
    if category != BUSINESS_CATEGORY_OTHER:
        return category, None
    text = " ".join(unicodedata.normalize("NFKC", other or "").split())
    if not 2 <= len(text) <= BUSINESS_CATEGORY_OTHER_MAX:
        raise ValueError("other")
    if any(unicodedata.category(c)[0] == "C" for c in text):
        raise ValueError("other")
    return category, text


class SlotQuery(BaseModel):
    tenant_id: int = Field(gt=0, description="ID del negocio")
    service_id: int = Field(gt=0, description="ID del servicio requerido")
    day: date = Field(description="Fecha a consultar YYYY-MM-DD")
    staff_id: int | None = Field(default=None, description="ID del profesional")

    @field_validator("day", mode="before")
    @classmethod
    def not_in_past(cls, v):
        d = date.fromisoformat(v) if isinstance(v, str) else v
        if d < date.today():
            raise ValueError("No se pueden consultar fechas pasadas")
        return d


class AvailableSlotsResponse(BaseModel):
    date: date
    service_duration_min: int
    timezone: str
    slots: list[str]


# Prefijo de idempotency_key reservado para las altas manuales del panel.
MANUAL_BOOKING_KEY_PREFIX = "panel-"


class BookingCreate(BaseModel):
    tenant_id: int
    service_id: int
    staff_id: int | None = None
    client_name: str = Field(max_length=200)
    client_phone: str
    start_time: datetime
    end_time: datetime | None = None
    idempotency_key: str

    @field_validator("idempotency_key")
    @classmethod
    def _reserve_panel_prefix(cls, value: str) -> str:
        # `panel-` es de las altas manuales del panel: su tope diario cuenta por
        # ese prefijo. Una reserva externa con esa clave podría gastar el cupo.
        if value.startswith(MANUAL_BOOKING_KEY_PREFIX):
            raise ValueError(
                f"La clave no puede empezar con '{MANUAL_BOOKING_KEY_PREFIX}'."
            )
        return value
