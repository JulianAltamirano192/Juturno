"""Tests de las notas internas por turno (POST /panel/agenda/{id}/notes)."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.csrf import generate_csrf_token
from app.main import app
from app.models import Booking, Service, Tenant
from app.session import create_session_token
from tests.conftest import TestingSessionLocal

TZ = "America/Argentina/Buenos_Aires"


async def _setup(status="pending"):
    """Crea un tenant con un turno para mañana y devuelve (tenant, booking, día local)."""
    async with TestingSessionLocal() as session:
        tenant = Tenant(
            name=f"Biz Notas {uuid.uuid4().hex[:6]}",
            slug=f"notas-{uuid.uuid4().hex[:8]}",
            owner_email=f"notas-{uuid.uuid4().hex[:8]}@test.com",
            password_hash="x",
            session_version=1,
            timezone=TZ,
        )
        session.add(tenant)
        await session.flush()
        service = Service(
            tenant_id=tenant.id,
            name="Servicio",
            duration_minutes=30,
            price=Decimal("5000.00"),
        )
        session.add(service)
        await session.flush()
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
        booking = Booking(
            tenant_id=tenant.id,
            service_id=service.id,
            client_name="Cliente Notas",
            client_phone="5491155555555",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price_at_booking=Decimal("5000.00"),
            idempotency_key=f"key-{uuid.uuid4().hex}",
            status=status,
        )
        session.add(booking)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(booking)
    day = booking.start_time.astimezone(ZoneInfo(TZ)).date().isoformat()
    return tenant, booking, day


async def _post_notes(tenant, booking_id, notes, day, with_csrf=True, session=True):
    csrf = generate_csrf_token()
    data = {"internal_notes": notes, "day": day}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        if session:
            client.cookies.set(
                "juturno_session",
                create_session_token(tenant.id, tenant.session_version),
            )
        if with_csrf:
            client.cookies.set("csrf_token", csrf)
            data["csrf_token"] = csrf
        return await client.post(
            f"/panel/agenda/{booking_id}/notes", data=data, follow_redirects=False
        )


async def _get_agenda(tenant, url):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(
            "juturno_session", create_session_token(tenant.id, tenant.session_version)
        )
        return await client.get(url)


async def _stored_notes(booking_id):
    async with TestingSessionLocal() as session:
        booking = await session.get(Booking, booking_id)
        return booking.internal_notes


@pytest.mark.asyncio
async def test_save_edit_and_clear_notes():
    tenant, booking, day = await _setup()

    res = await _post_notes(tenant, booking.id, "  Trae su propio material  ", day)
    assert res.status_code == 303
    assert res.headers["location"] == f"/panel/agenda?day={day}"
    assert await _stored_notes(booking.id) == "Trae su propio material"

    res = await _post_notes(tenant, booking.id, "Línea 1\nLínea 2", day)
    assert res.status_code == 303
    assert await _stored_notes(booking.id) == "Línea 1\nLínea 2"

    # Vacío o solo espacios → sin nota (NULL)
    res = await _post_notes(tenant, booking.id, "   ", day)
    assert res.status_code == 303
    assert await _stored_notes(booking.id) is None


@pytest.mark.asyncio
async def test_notes_do_not_change_status():
    tenant, booking, day = await _setup(status="pending")
    await _post_notes(tenant, booking.id, "Nota", day)
    async with TestingSessionLocal() as session:
        stored = await session.get(Booking, booking.id)
    assert stored.status == "pending"


@pytest.mark.asyncio
async def test_notes_exactly_500_chars_ok():
    tenant, booking, day = await _setup()
    res = await _post_notes(tenant, booking.id, "a" * 500, day)
    assert res.status_code == 303
    assert await _stored_notes(booking.id) == "a" * 500


@pytest.mark.asyncio
async def test_notes_too_long_not_saved_and_error_shown():
    tenant, booking, day = await _setup()
    await _post_notes(tenant, booking.id, "Nota previa", day)

    res = await _post_notes(tenant, booking.id, "a" * 501, day)
    assert res.status_code == 303
    location = res.headers["location"]
    assert "notes=too_long" in location
    assert await _stored_notes(booking.id) == "Nota previa"

    page = await _get_agenda(tenant, location)
    assert page.status_code == 200
    assert "banner-danger" in page.text
    assert "500 caracteres" in page.text


@pytest.mark.asyncio
async def test_notes_other_tenant_booking_404_and_unchanged():
    _, booking, day = await _setup()
    other, _, _ = await _setup()
    res = await _post_notes(other, booking.id, "Intruso", day)
    assert res.status_code == 404
    assert await _stored_notes(booking.id) is None


@pytest.mark.asyncio
async def test_notes_require_csrf():
    tenant, booking, day = await _setup()
    res = await _post_notes(tenant, booking.id, "Nota", day, with_csrf=False)
    assert res.status_code == 403
    assert await _stored_notes(booking.id) is None


@pytest.mark.asyncio
async def test_notes_require_session():
    tenant, booking, day = await _setup()
    res = await _post_notes(tenant, booking.id, "Nota", day, session=False)
    assert res.status_code == 303
    assert "/login" in res.headers["location"]
    assert await _stored_notes(booking.id) is None


@pytest.mark.asyncio
async def test_notes_on_cancelled_booking():
    tenant, booking, day = await _setup(status="cancelled")
    res = await _post_notes(tenant, booking.id, "Avisó tarde", day)
    assert res.status_code == 303
    assert await _stored_notes(booking.id) == "Avisó tarde"


@pytest.mark.asyncio
async def test_agenda_renders_note_escaped():
    tenant, booking, day = await _setup()
    await _post_notes(tenant, booking.id, "<script>alert(1)</script>", day)

    page = await _get_agenda(tenant, f"/panel/agenda?day={day}")
    assert page.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert f'action="/panel/agenda/{booking.id}/notes"' in page.text
    assert 'maxlength="500"' in page.text


@pytest.mark.asyncio
async def test_notes_crlf_counts_as_one_char_per_line_break():
    # El navegador manda los saltos del textarea como CRLF, pero su maxlength
    # los cuenta como uno: 490 letras + 10 saltos = 500 (510 crudos).
    tenant, booking, day = await _setup()
    lines = ["a" * 45] * 10 + ["a" * 40]
    raw = "\r\n".join(lines)
    assert len(raw) == 510
    res = await _post_notes(tenant, booking.id, raw, day)
    assert res.status_code == 303
    assert "notes=" not in res.headers["location"]
    assert await _stored_notes(booking.id) == "\n".join(lines)


@pytest.mark.asyncio
async def test_notes_with_control_chars_not_saved_and_error_shown():
    tenant, booking, day = await _setup()
    await _post_notes(tenant, booking.id, "Nota previa", day)

    res = await _post_notes(tenant, booking.id, "Hola\x00mundo", day)
    assert res.status_code == 303
    location = res.headers["location"]
    assert "notes=invalid" in location
    assert await _stored_notes(booking.id) == "Nota previa"

    page = await _get_agenda(tenant, location)
    assert page.status_code == 200
    assert "La nota tiene caracteres no permitidos." in page.text


@pytest.mark.asyncio
async def test_notes_allow_tabs_and_newlines():
    tenant, booking, day = await _setup()
    res = await _post_notes(tenant, booking.id, "a\tb\nc", day)
    assert res.status_code == 303
    assert await _stored_notes(booking.id) == "a\tb\nc"


@pytest.mark.asyncio
async def test_notes_allow_composed_emoji():
    # El emoji compuesto usa zero-width joiner (categoría Cf): no es un
    # carácter de control y la nota es privada del dueño.
    tenant, booking, day = await _setup()
    note = "Viene con 👨‍👩‍👧"
    res = await _post_notes(tenant, booking.id, note, day)
    assert res.status_code == 303
    assert await _stored_notes(booking.id) == note
