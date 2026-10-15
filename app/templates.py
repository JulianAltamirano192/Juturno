from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.logo import logo_version
from app.models import (
    BUSINESS_CATEGORIES,
    BUSINESS_CATEGORY_OTHER_MAX,
    SERVICE_BUFFER_CHOICES,
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def format_money(value: Decimal | float | str | None) -> str:
    """Formatea un monto en pesos al estilo argentino: '$ 18.000' o '$ 18.000,50'."""
    if value is None:
        return ""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return str(value)
    integer, cents = f"{amount:,.2f}".split(".")
    integer = integer.replace(",", ".")
    return f"$ {integer}" if cents == "00" else f"$ {integer},{cents}"


templates.env.filters["money"] = format_money
templates.env.globals["business_categories"] = BUSINESS_CATEGORIES
templates.env.globals["business_category_other_max"] = BUSINESS_CATEGORY_OTHER_MAX
templates.env.globals["service_buffer_choices"] = SERVICE_BUFFER_CHOICES
# Función (el contexto de algunas páginas trae "logo_version" ya calculado).
templates.env.globals["logo_version_of"] = logo_version
