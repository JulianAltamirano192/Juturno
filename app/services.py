from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP


def effective_deposit(price: Decimal, deposit_amount: Decimal | None) -> Decimal:
    """Monto de seña efectivo: el explícito, o 30% del precio si es None."""
    if deposit_amount is not None:
        return deposit_amount
    return (price * Decimal("0.30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_available_slots(
    windows: list[tuple[datetime, datetime]],
    bookings: list[tuple[datetime, datetime]],
    duration_min: int,
    granularity_min: int = 30,
) -> list[str]:
    """
    Calcula los slots libres para una lista de ventanas de atención.

    Args:
        windows: Lista de (window_start, window_end) que representan los
            intervalos de atención del día (p.ej. mañana + tarde si hay
            corte al mediodía). Todos los datetimes deben tener tzinfo
            consistente con los de `bookings`.
        bookings: Lista de (start, end) de reservas ya confirmadas/pendientes.
        duration_min: Duración del servicio en minutos.
        granularity_min: Grilla de oferta de turnos (default 30 min).

    Returns:
        Lista de strings "HH:MM" de slots disponibles, en orden cronológico.

    Nota: NO LLAMAR por request con cientos de ventanas; está pensado para
    un único día de un único tenant/staff (pocas ventanas, pocas reservas).
    """
    # Normalizar bookings: merge solapamientos para simplificar la búsqueda
    bookings = sorted(bookings, key=lambda x: x[0])
    merged_busy: list[list[datetime]] = []
    for b_start, b_end in bookings:
        if not merged_busy:
            merged_busy.append([b_start, b_end])
        else:
            last_b = merged_busy[-1]
            if b_start <= last_b[1]:
                last_b[1] = max(last_b[1], b_end)
            else:
                merged_busy.append([b_start, b_end])

    duration = timedelta(minutes=duration_min)
    granularity = timedelta(minutes=granularity_min)
    gran_sec = granularity.total_seconds()

    slots: list[str] = []

    for window_start, window_end in windows:
        # Obtener gaps libres dentro de esta ventana
        gaps = []
        prev_end = window_start

        for b_start, b_end in merged_busy:
            # Solo nos importan reservas que se crucen con esta ventana
            effective_start = max(b_start, window_start)
            effective_end = min(b_end, window_end)
            if effective_start >= effective_end:
                continue
            if effective_start > prev_end:
                gaps.append((prev_end, effective_start))
            prev_end = max(prev_end, effective_end)

        if prev_end < window_end:
            gaps.append((prev_end, window_end))

        for g_start, g_end in gaps:
            # Alinear el primer slot a la grilla global (anclada al inicio de ventana)
            offset = (g_start - window_start).total_seconds()
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


async def resolve_day_windows(
    session,
    tenant_id: int,
    day_of_week: int,
    day_date,
    tenant_timezone,
    staff_id=None,
) -> list[tuple[datetime, datetime]]:
    """
    Devuelve la lista de ventanas (window_start, window_end) para el día dado.

    Lógica:
    1. Si se pasa staff_id, busca filas de BusinessHours para (tenant_id, staff_id, day_of_week).
       Si hay → usa esas ventanas.
       Si no hay → cae al horario general del negocio (staff_id IS NULL).
    2. Para el horario general: busca filas de (tenant_id, staff_id=NULL, day_of_week).
    3. Si el tenant/staff no tiene NINGUNA fila en business_hours en cualquier día
       → aplica fallback estático 09:00-18:00.
       Si tiene al menos una fila pero no para este día → día cerrado, lista vacía.
    """
    from sqlalchemy import select, func, and_
    from app.models import BusinessHours
    from datetime import time as time_type

    FALLBACK_START = time_type(9, 0)
    FALLBACK_END = time_type(18, 0)

    def make_windows(rows) -> list[tuple[datetime, datetime]]:
        windows = []
        for row in rows:
            ws = datetime.combine(day_date, row.start_time, tzinfo=tenant_timezone)
            we = datetime.combine(day_date, row.end_time, tzinfo=tenant_timezone)
            windows.append((ws, we))
        windows.sort(key=lambda x: x[0])
        return windows

    # --- Intento 1: horario específico de staff (si aplica) ---
    if staff_id is not None:
        staff_day_stmt = select(BusinessHours).where(
            and_(
                BusinessHours.tenant_id == tenant_id,
                BusinessHours.staff_id == staff_id,
                BusinessHours.day_of_week == day_of_week,
            )
        )
        staff_day_rows = (await session.execute(staff_day_stmt)).scalars().all()
        if staff_day_rows:
            return make_windows(staff_day_rows)

        # ¿Tiene el staff ALGUNA fila cargada (en cualquier día)?
        staff_any_stmt = select(func.count()).where(
            and_(
                BusinessHours.tenant_id == tenant_id,
                BusinessHours.staff_id == staff_id,
            )
        )
        staff_any_count = (await session.execute(staff_any_stmt)).scalar_one()
        if staff_any_count > 0:
            # Staff tiene filas pero no para hoy → cerrado
            return []

        # Staff sin ninguna fila → caemos al horario general del negocio

    # --- Intento 2: horario general del negocio (staff_id IS NULL) ---
    biz_day_stmt = select(BusinessHours).where(
        and_(
            BusinessHours.tenant_id == tenant_id,
            BusinessHours.staff_id.is_(None),
            BusinessHours.day_of_week == day_of_week,
        )
    )
    biz_day_rows = (await session.execute(biz_day_stmt)).scalars().all()
    if biz_day_rows:
        return make_windows(biz_day_rows)

    # ¿Tiene el negocio ALGUNA fila general cargada (en cualquier día)?
    biz_any_stmt = select(func.count()).where(
        and_(
            BusinessHours.tenant_id == tenant_id,
            BusinessHours.staff_id.is_(None),
        )
    )
    biz_any_count = (await session.execute(biz_any_stmt)).scalar_one()
    if biz_any_count > 0:
        # Negocio tiene filas generales pero no para hoy → cerrado
        return []

    # --- Fallback: negocio sin ninguna fila configurada → 09-18 ---
    ws = datetime.combine(day_date, FALLBACK_START, tzinfo=tenant_timezone)
    we = datetime.combine(day_date, FALLBACK_END, tzinfo=tenant_timezone)
    return [(ws, we)]
