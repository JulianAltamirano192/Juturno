from datetime import datetime
from app.services import calculate_available_slots


BASE = "2025-03-15"


def dt(t: str) -> datetime:
    return datetime.fromisoformat(f"{BASE}T{t}")


def test_single_window_design_example():
    """Reproduces the original design example with the new list-of-windows API."""
    windows = [(dt("09:00:00"), dt("18:00:00"))]
    bookings = [
        (dt("10:00:00"), dt("11:00:00")),
        (dt("11:30:00"), dt("12:30:00")),
        (dt("14:00:00"), dt("15:00:00")),
    ]

    result = calculate_available_slots(
        windows=windows,
        bookings=bookings,
        duration_min=60,
        granularity_min=30,
    )

    expected = ["09:00", "12:30", "13:00", "15:00", "15:30", "16:00", "16:30", "17:00"]
    assert result == expected, f"Esperaba {expected}, obtuve {result}"


def test_no_windows_returns_empty():
    """Un día cerrado (lista de ventanas vacía) devuelve lista vacía."""
    result = calculate_available_slots(
        windows=[],
        bookings=[],
        duration_min=60,
        granularity_min=30,
    )
    assert result == []


def test_split_schedule_morning_afternoon():
    """Horario cortado al mediodía: 09-13 y 15-19, sin reservas."""
    windows = [(dt("09:00:00"), dt("13:00:00")), (dt("15:00:00"), dt("19:00:00"))]
    result = calculate_available_slots(
        windows=windows,
        bookings=[],
        duration_min=60,
        granularity_min=30,
    )
    expected = [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
        "11:00",
        "11:30",
        "12:00",  # mañana
        "15:00",
        "15:30",
        "16:00",
        "16:30",
        "17:00",
        "17:30",
        "18:00",  # tarde
    ]
    assert result == expected, f"Esperaba {expected}, obtuve {result}"


def test_booking_spanning_gap_does_not_leak_into_next_window():
    """Una reserva que cae en el descanso al mediodía no 'contagia' la ventana de tarde."""
    windows = [(dt("09:00:00"), dt("13:00:00")), (dt("15:00:00"), dt("19:00:00"))]
    # Reserva de 12:00 a 16:00 (cruza el corte del mediodía)
    bookings = [(dt("12:00:00"), dt("16:00:00"))]
    result = calculate_available_slots(
        windows=windows,
        bookings=bookings,
        duration_min=60,
        granularity_min=30,
    )
    expected = [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
        "11:00",  # mañana libre
        # 11:30 y 12:00 ya no caben (12:00-13:00 bloqueado)
        "16:00",
        "16:30",
        "17:00",
        "17:30",
        "18:00",  # tarde libre desde 16:00
    ]
    assert result == expected, f"Esperaba {expected}, obtuve {result}"


def test_no_slots_when_fully_booked():
    """Si la única ventana está completamente ocupada, no hay slots."""
    windows = [(dt("09:00:00"), dt("10:00:00"))]
    bookings = [(dt("09:00:00"), dt("10:00:00"))]
    result = calculate_available_slots(
        windows=windows,
        bookings=bookings,
        duration_min=60,
        granularity_min=30,
    )
    assert result == []
