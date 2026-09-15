from datetime import datetime, timedelta
from typing import List


def calculate_available_slots(
    window_start: datetime,
    window_end: datetime,
    bookings: List[tuple[datetime, datetime]],
    duration_min: int,
    granularity_min: int = 30,
) -> List[str]:
    """
    Calcula los slots libres usando el enfoque 'grid sobre intervalos gaps'.
    """
    bookings.sort(key=lambda x: x[0])
    merged_busy: List[List[datetime]] = []

    for b_start, b_end in bookings:
        if not merged_busy:
            merged_busy.append([b_start, b_end])
        else:
            last_b = merged_busy[-1]
            if b_start <= last_b[1]:
                last_b[1] = max(last_b[1], b_end)
            else:
                merged_busy.append([b_start, b_end])

    gaps = []
    prev_end = window_start

    for b_start, b_end in merged_busy:
        if b_start > prev_end:
            gaps.append((prev_end, b_start))
        prev_end = max(prev_end, b_end)

    if prev_end < window_end:
        gaps.append((prev_end, window_end))

    slots = []
    duration = timedelta(minutes=duration_min)
    granularity = timedelta(minutes=granularity_min)

    for g_start, g_end in gaps:
        offset = (g_start - window_start).total_seconds()
        gran_sec = granularity.total_seconds()
        remainder = offset % gran_sec

        if remainder == 0:
            first_slot = g_start
        else:
            first_slot = g_start + timedelta(seconds=(gran_sec - remainder))

        t = first_slot
        while t + duration <= g_end:
            slots.append(t.strftime("%H:%M"))
            t += granularity

    return slots
