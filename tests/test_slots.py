from datetime import datetime
from app.services import calculate_available_slots


def test_calculate_available_slots_exact_design_example():
    base_date = "2025-03-15"
    window_start = datetime.fromisoformat(f"{base_date}T09:00:00")
    window_end = datetime.fromisoformat(f"{base_date}T18:00:00")

    bookings = [
        (
            datetime.fromisoformat(f"{base_date}T10:00:00"),
            datetime.fromisoformat(f"{base_date}T11:00:00"),
        ),
        (
            datetime.fromisoformat(f"{base_date}T11:30:00"),
            datetime.fromisoformat(f"{base_date}T12:30:00"),
        ),
        (
            datetime.fromisoformat(f"{base_date}T14:00:00"),
            datetime.fromisoformat(f"{base_date}T15:00:00"),
        ),
    ]

    result_slots = calculate_available_slots(
        window_start=window_start,
        window_end=window_end,
        bookings=bookings,
        duration_min=60,
        granularity_min=30,
    )

    expected_slots = [
        "09:00",
        "12:30",
        "13:00",
        "15:00",
        "15:30",
        "16:00",
        "16:30",
        "17:00",
    ]

    assert (
        result_slots == expected_slots
    ), f"Esperaba {expected_slots}, obtuve {result_slots}"
