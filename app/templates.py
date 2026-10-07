from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi.templating import Jinja2Templates

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
