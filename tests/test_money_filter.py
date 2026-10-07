from decimal import Decimal

from app.templates import format_money


def test_format_money_integer_amount():
    assert format_money(Decimal("18000.00")) == "$ 18.000"


def test_format_money_keeps_cents():
    assert format_money(Decimal("1234.5")) == "$ 1.234,50"


def test_format_money_small_and_none():
    assert format_money(900) == "$ 900"
    assert format_money(None) == ""
