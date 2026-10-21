from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal


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
    0. Si el día está bloqueado (BlockedPeriod del negocio o de ese staff_id)
       → lista vacía.
    1. Si se pasa staff_id, busca filas de BusinessHours para (tenant_id, staff_id, day_of_week).
       Si hay → usa esas ventanas.
       Si no hay → cae al horario general del negocio (staff_id IS NULL).
    2. Para el horario general: busca filas de (tenant_id, staff_id=NULL, day_of_week).
    3. Si el tenant/staff no tiene NINGUNA fila en business_hours en cualquier día
       → aplica fallback estático 09:00-18:00.
       Si tiene al menos una fila pero no para este día → día cerrado, lista vacía.
    """
    from datetime import time as time_type

    from sqlalchemy import and_, func, or_, select

    from app.models import BlockedPeriod, BusinessHours

    # Día bloqueado del negocio (o de este profesional) → sin ventanas.
    blocked_owner = BlockedPeriod.staff_id.is_(None)
    if staff_id is not None:
        blocked_owner = or_(blocked_owner, BlockedPeriod.staff_id == staff_id)
    blocked_stmt = select(func.count()).where(
        BlockedPeriod.tenant_id == tenant_id,
        BlockedPeriod.start_date <= day_date,
        BlockedPeriod.end_date >= day_date,
        blocked_owner,
    )
    if (await session.execute(blocked_stmt)).scalar_one():
        return []

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


async def compute_available_slots(
    *,
    session,
    tenant_id: int,
    service,
    day,
    staff_id,
    tenant_timezone,
    horizon_days: int,
) -> list[str]:
    """Lógica compartida de cálculo de slots. El caller valida auth y tenant."""
    from datetime import time

    from sqlalchemy import and_, or_, select

    from app.models import Booking

    now_local = datetime.now(tenant_timezone)
    today_local = now_local.date()
    if day > today_local + timedelta(days=horizon_days):
        return []

    windows = await resolve_day_windows(
        session=session,
        tenant_id=tenant_id,
        day_of_week=day.weekday(),
        day_date=day,
        tenant_timezone=tenant_timezone,
        staff_id=staff_id,
    )

    if not windows:
        return []

    buffer = timedelta(minutes=service.buffer_minutes)
    day_start = min(w[0] for w in windows)
    day_end = max(w[1] for w in windows)

    stmt = select(Booking).where(
        and_(
            Booking.tenant_id == tenant_id,
            Booking.status.in_(["pending", "confirmed"]),
            Booking.start_time < day_end + buffer,
            Booking.end_time > day_start,
        )
    )
    if staff_id:
        # Un turno sin profesional ocupa a todos: el EXCLUDE no lo compara con
        # los asignados (COALESCE(staff_id, -1)); al crear lo frena
        # has_overlapping_booking.
        stmt = stmt.where(or_(Booking.staff_id == staff_id, Booking.staff_id.is_(None)))

    bookings_db = (await session.execute(stmt)).scalars().all()

    # El tiempo libre del servicio nuevo no puede pisar al turno siguiente:
    # cada turno existente "empieza" buffer minutos antes. El tiempo libre de
    # los existentes ya está en su end_time.
    bookings_intervals = [
        (
            b.start_time.astimezone(tenant_timezone) - buffer,
            b.end_time.astimezone(tenant_timezone),
        )
        for b in bookings_db
    ]

    slots = calculate_available_slots(
        windows=windows,
        bookings=bookings_intervals,
        duration_min=service.duration_minutes,
        granularity_min=30,
    )

    if day == today_local:
        slots = [
            s
            for s in slots
            if datetime.combine(
                day, time(*map(int, s.split(":"))), tzinfo=tenant_timezone
            )
            >= now_local
        ]

    return slots


async def is_bookable_start(
    *,
    session,
    tenant_id: int,
    service,
    start_time: datetime,
    staff_id,
    tenant_timezone,
    horizon_days: int,
) -> bool:
    """True si start_time es un turno que la grilla ofrecería (futuro, dentro
    del horario, alineado y con lugar para la duración), sin mirar reservas:
    la superposición la resuelve el EXCLUDE (409). start_time debe tener tz."""
    try:
        local = start_time.astimezone(tenant_timezone)
    except OverflowError:  # year 1 / 9999 with an explicit offset
        return False
    now_local = datetime.now(tenant_timezone)
    if local < now_local or local.second or local.microsecond:
        return False
    if local.date() > now_local.date() + timedelta(days=horizon_days):
        return False

    windows = await resolve_day_windows(
        session=session,
        tenant_id=tenant_id,
        day_of_week=local.weekday(),
        day_date=local.date(),
        tenant_timezone=tenant_timezone,
        staff_id=staff_id,
    )
    slots = calculate_available_slots(
        windows=windows,
        bookings=[],
        duration_min=service.duration_minutes,
        granularity_min=30,
    )
    return local.strftime("%H:%M") in slots


async def active_staff(session, tenant_id: int) -> list:
    """Profesionales activos del negocio, en orden de alta."""
    from sqlalchemy import select

    from app.models import Staff

    stmt = (
        select(Staff)
        .where(Staff.tenant_id == tenant_id, Staff.is_active.is_(True))
        .order_by(Staff.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def compute_any_staff_slots(*, staff_ids: list[int], **kwargs) -> list[str]:
    """ "Sin preferencia": unión de los turnos libres de cada profesional."""
    slots: set[str] = set()
    for staff_id in staff_ids:
        slots.update(await compute_available_slots(staff_id=staff_id, **kwargs))
    return sorted(slots)


async def free_staff_for(
    *,
    session,
    tenant_id: int,
    start_time: datetime,
    end_time: datetime,
    staff: list,
    tenant_timezone,
) -> list:
    """De `staff`, los que no tienen turno en [start_time, end_time), primero el
    de menos turnos ese día (desempata el más antiguo). Vacío si un turno sin
    profesional pisa el horario, porque ocupa a todos. No mira horarios: eso
    lo valida is_bookable_start antes."""
    from sqlalchemy import func, select
    from sqlmodel import col

    from app.models import Booking

    active = Booking.status.in_(["pending", "confirmed"])
    overlapping = select(col(Booking.staff_id)).where(
        Booking.tenant_id == tenant_id,
        active,
        Booking.start_time < end_time,
        Booking.end_time > start_time,
    )
    busy = set((await session.execute(overlapping)).scalars().all())
    if None in busy:
        return []
    candidates = [member for member in staff if member.id not in busy]
    if len(candidates) < 2:
        return candidates

    local_day = start_time.astimezone(tenant_timezone).date()
    day_start = datetime.combine(local_day, datetime.min.time(), tenant_timezone)
    load_stmt = (
        select(col(Booking.staff_id), func.count())
        .where(
            Booking.tenant_id == tenant_id,
            active,
            col(Booking.staff_id).in_([m.id for m in candidates]),
            Booking.start_time >= day_start,
            Booking.start_time < day_start + timedelta(days=1),
        )
        .group_by(Booking.staff_id)
    )
    load = dict((await session.execute(load_stmt)).all())
    return sorted(candidates, key=lambda m: (load.get(m.id, 0), m.id))


async def has_overlapping_booking(
    *, session, tenant_id: int, start_time: datetime, end_time: datetime, staff_id
) -> bool:
    """True si [start_time, end_time) pisa un turno activo que lo ocupa.

    Un turno sin profesional ocupa a todos y uno asignado ocupa a su
    profesional. El EXCLUDE (COALESCE(staff_id, -1)) no compara asignados con
    sin asignar, así que este chequeo cubre ese hueco al crear.
    ponytail: chequeo y INSERT no son atómicos; un turno sin profesional y uno
    asignado creados a la vez pueden encimarse. Arreglo de fondo: asignar
    siempre un profesional (o lock por tenant) si llega a pasar.
    """
    from sqlalchemy import func, or_, select

    from app.models import Booking

    stmt = select(func.count()).where(
        Booking.tenant_id == tenant_id,
        Booking.status.in_(["pending", "confirmed"]),
        Booking.start_time < end_time,
        Booking.end_time > start_time,
    )
    if staff_id is not None:
        stmt = stmt.where(or_(Booking.staff_id == staff_id, Booking.staff_id.is_(None)))
    return (await session.execute(stmt)).scalar_one() > 0


# Ids por consulta: asyncpg admite hasta 32767 parámetros (un CSV de un año
# de un negocio grande puede tener más turnos).
_EXTRAS_BATCH = 10_000


async def extra_services_by_booking(
    session, tenant_id: int, booking_ids: list[int]
) -> dict[int, list]:
    """Servicios extra de cada turno (ítem 43), en orden; los turnos de un solo
    servicio no aparecen. Solo servicios del tenant, por las dudas."""
    if not booking_ids:
        return {}
    from sqlalchemy import select
    from sqlmodel import col

    from app.models import BookingExtraService, Service

    extras: dict[int, list] = {}
    for i in range(0, len(booking_ids), _EXTRAS_BATCH):
        stmt = (
            select(col(BookingExtraService.booking_id), Service)
            .join(Service, BookingExtraService.service_id == Service.id)
            .where(
                col(BookingExtraService.booking_id).in_(
                    booking_ids[i : i + _EXTRAS_BATCH]
                ),
                Service.tenant_id == tenant_id,
            )
            .order_by(BookingExtraService.booking_id, BookingExtraService.position)
        )
        for booking_id, service in await session.execute(stmt):
            extras.setdefault(booking_id, []).append(service)
    return extras


def combo_name(main_name: str, extras: list | None) -> str:
    """Nombre del turno ("Lavado + Encerado"): el principal y los extra, en orden."""
    return " + ".join([main_name, *(s.name for s in extras or [])])
