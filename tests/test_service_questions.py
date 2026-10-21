"""
Preguntas personalizadas por servicio (Fase 5, ítem 42).

T1: el panel de servicios permite cargar hasta 3 preguntas de texto, cada una
obligatoria u opcional (Service.questions).
T2: la reserva pública y la API guardan las respuestas en el turno
(Booking.answers); solo la pública exige las obligatorias.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Booking, Service, Tenant
from tests.test_booking_start_validation import ENDPOINTS, TOMORROW, _setup, _utc
from tests.test_booking_start_validation import _post as _post_booking
from tests.test_panel_mp_connect import _create_tenant, _make_cookie
from tests.test_service_buffer import _post, _service_form


async def _only_service(db_session, tenant) -> Service:
    rows = await db_session.execute(
        select(Service).where(Service.tenant_id == tenant.id)
    )
    return rows.scalar_one()


def _tenant_suffix(name: str) -> str:
    return f"q-{name}-{uuid.uuid4().hex[:6]}"


@pytest.mark.asyncio
async def test_new_service_saves_questions(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("new"))
    form = _service_form(
        question_1="  ¿Qué modelo   es tu\nauto? ",
        question_1_required="on",
        question_2="",
        question_2_required="on",  # sin texto: la fila se ignora
        question_3="¿Alguna indicación?",
    )

    res = await _post(client, tenant, "/panel/services/new", form)

    assert res.status_code == 303
    service = await _only_service(db_session, tenant)
    assert service.questions == [
        {"text": "¿Qué modelo es tu auto?", "required": True},
        {"text": "¿Alguna indicación?", "required": False},
    ]

    cookies = {"juturno_session": _make_cookie(tenant)}
    client.cookies.clear()
    page = await client.get(f"/panel/services/{service.id}/edit", cookies=cookies)
    assert 'value="¿Qué modelo es tu auto?"' in page.text
    assert 'id="question_1_required" name="question_1_required" value="on" checked' in (
        page.text
    )
    assert 'id="question_2_required" name="question_2_required" value="on">' in (
        page.text
    )
    listing = await client.get("/panel/services", cookies=cookies)
    assert "2 preguntas" in listing.text


@pytest.mark.asyncio
async def test_new_service_without_questions_has_empty_list(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("none"))

    res = await _post(client, tenant, "/panel/services/new", _service_form())

    assert res.status_code == 303
    assert (await _only_service(db_session, tenant)).questions == []


@pytest.mark.asyncio
async def test_edit_replaces_and_clears_questions(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("edit"))
    await _post(
        client, tenant, "/panel/services/new", _service_form(question_1="¿Primera?")
    )
    service = await _only_service(db_session, tenant)
    url = f"/panel/services/{service.id}/edit"

    res = await _post(
        client,
        tenant,
        url,
        _service_form(question_1="", question_2="¿Segunda?", question_2_required="on"),
    )
    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.questions == [{"text": "¿Segunda?", "required": True}]

    res = await _post(
        client, tenant, url, _service_form(question_1="", question_2="", question_3="")
    )
    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.questions == []


@pytest.mark.asyncio
async def test_edit_without_question_fields_keeps_them(client, db_session):
    """Un formulario viejo (sin los campos de preguntas) no las borra."""
    tenant = await _create_tenant(db_session, _tenant_suffix("keep"))
    await _post(
        client, tenant, "/panel/services/new", _service_form(question_1="¿Queda?")
    )
    service = await _only_service(db_session, tenant)

    res = await _post(
        client, tenant, f"/panel/services/{service.id}/edit", _service_form()
    )

    assert res.status_code == 303
    await db_session.refresh(service)
    assert service.questions == [{"text": "¿Queda?", "required": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, error",
    [
        ("x" * 121, "La pregunta puede tener hasta 120 caracteres."),
        ("¿Hola\x00?", "La pregunta tiene caracteres no permitidos."),
    ],
)
async def test_invalid_question_is_rejected(client, db_session, text, error):
    tenant = await _create_tenant(db_session, _tenant_suffix("bad"))

    res = await _post(
        client, tenant, "/panel/services/new", _service_form(question_2=text)
    )

    assert res.status_code == 200
    assert error in res.text
    rows = await db_session.execute(
        select(Service).where(Service.tenant_id == tenant.id)
    )
    assert rows.scalars().all() == []


@pytest.mark.asyncio
async def test_question_of_exactly_120_chars_is_accepted(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("max"))

    res = await _post(
        client, tenant, "/panel/services/new", _service_form(question_1="x" * 120)
    )

    assert res.status_code == 303
    assert (await _only_service(db_session, tenant)).questions == [
        {"text": "x" * 120, "required": False}
    ]


QUESTIONS = [
    {"text": "¿Qué modelo es tu auto?", "required": True},
    {"text": "¿Alguna indicación?", "required": False},
]


START = _utc(TOMORROW, 10)


async def _setup_with_questions(db_session, questions=QUESTIONS):
    tenant, service, raw_key = await _setup(db_session)
    service.questions = questions
    db_session.add(service)
    await db_session.commit()
    return tenant, service, raw_key


async def _saved_answers(db_session, tenant):
    rows = await db_session.execute(
        select(Booking).where(Booking.tenant_id == tenant.id)
    )
    return [b.answers for b in rows.scalars().all()]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_answers_are_saved_with_their_question(client, db_session, endpoint):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client,
        endpoint,
        raw_key,
        tenant,
        service,
        START,
        answers=["  Fiat   Cronos\n2020 ", ""],
    )

    assert res.status_code == 201, res.text
    assert await _saved_answers(db_session, tenant) == [
        [{"question": "¿Qué modelo es tu auto?", "answer": "Fiat Cronos 2020"}]
    ]


@pytest.mark.asyncio
async def test_public_booking_requires_required_answers(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["", "Algo"]
    )

    assert res.status_code == 422
    assert res.json()["detail"] == "Falta responder: ¿Qué modelo es tu auto?"
    assert await _saved_answers(db_session, tenant) == []


@pytest.mark.asyncio
async def test_public_booking_without_answers_field_requires_them(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(client, "public", raw_key, tenant, service, START)

    assert res.status_code == 422
    assert res.json()["detail"] == "Falta responder: ¿Qué modelo es tu auto?"


@pytest.mark.asyncio
async def test_api_booking_does_not_require_answers(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(client, "api", raw_key, tenant, service, START)

    assert res.status_code == 201, res.text
    assert await _saved_answers(db_session, tenant) == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "answers, detail",
    [
        (["a", "b", "c"], "Hay más respuestas que preguntas para este servicio."),
        (["x" * 201], "Cada respuesta puede tener hasta 200 caracteres."),
        (["Fiat\x00"], "Una respuesta tiene caracteres no permitidos."),
        (["Fiat\u202e"], "Una respuesta tiene caracteres no permitidos."),
    ],
)
async def test_invalid_answers_are_rejected(
    client, db_session, endpoint, answers, detail
):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, endpoint, raw_key, tenant, service, START, answers=answers
    )

    assert res.status_code == 422
    assert res.json()["detail"] == detail
    assert await _saved_answers(db_session, tenant) == []


@pytest.mark.asyncio
async def test_answer_of_exactly_200_chars_is_accepted(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["x" * 200]
    )

    assert res.status_code == 201, res.text
    assert await _saved_answers(db_session, tenant) == [
        [{"question": "¿Qué modelo es tu auto?", "answer": "x" * 200}]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_service_without_questions_keeps_working(client, db_session, endpoint):
    tenant, service, raw_key = await _setup(db_session)

    res = await _post_booking(client, endpoint, raw_key, tenant, service, START)

    assert res.status_code == 201, res.text
    assert await _saved_answers(db_session, tenant) == [None]


@pytest.mark.asyncio
async def test_editing_questions_does_not_change_saved_answers(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)
    await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["Fiat"]
    )

    service.questions = [{"text": "¿Otra pregunta?", "required": True}]
    db_session.add(service)
    await db_session.commit()

    assert await _saved_answers(db_session, tenant) == [
        [{"question": "¿Qué modelo es tu auto?", "answer": "Fiat"}]
    ]


@pytest.mark.asyncio
async def test_public_page_sends_questions_escaped(client, db_session):
    tenant = Tenant(
        name="Negocio preguntas",
        slug=f"preguntas-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
    )
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        Service(
            tenant_id=tenant.id,
            name="Servicio",
            duration_minutes=30,
            price=Decimal(1000),
            questions=[{"text": "</script><b>¿Seguro?", "required": True}],
        )
    )
    await db_session.commit()

    page = await client.get(f"/t/{tenant.slug}")

    assert page.status_code == 200
    assert "</script><b>" not in page.text
    assert '"questions": [{"required": true, "text": "\\u003c/script\\u003e' in (
        page.text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_booking_without_answers_stores_sql_null(client, db_session, endpoint):
    """Sin respuestas la columna queda en NULL de SQL, no en el JSON `null`."""
    tenant, service, raw_key = await _setup(db_session)

    res = await _post_booking(client, endpoint, raw_key, tenant, service, START)

    assert res.status_code == 201, res.text
    nulls = await db_session.execute(
        select(Booking.id).where(
            Booking.tenant_id == tenant.id, Booking.answers.is_(None)
        )
    )
    assert len(nulls.all()) == 1


@pytest.mark.asyncio
async def test_blank_required_answer_is_rejected(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["   \n "]
    )

    assert res.status_code == 422
    assert res.json()["detail"] == "Falta responder: ¿Qué modelo es tu auto?"


@pytest.mark.asyncio
async def test_answers_keep_what_the_client_wrote(client, db_session):
    """Solo se colapsan los espacios: no se normaliza el texto (², …)."""
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["Fiat 500² …"]
    )

    assert res.status_code == 201, res.text
    assert await _saved_answers(db_session, tenant) == [
        [{"question": "¿Qué modelo es tu auto?", "answer": "Fiat 500² …"}]
    ]


@pytest.mark.asyncio
async def test_empty_middle_row_is_compacted(client, db_session):
    tenant = await _create_tenant(db_session, _tenant_suffix("mid"))

    res = await _post(
        client,
        tenant,
        "/panel/services/new",
        _service_form(question_1="", question_2="¿Segunda?", question_3="¿Tercera?"),
    )

    assert res.status_code == 303
    assert (await _only_service(db_session, tenant)).questions == [
        {"text": "¿Segunda?", "required": False},
        {"text": "¿Tercera?", "required": False},
    ]


@pytest.mark.asyncio
async def test_huge_answer_is_rejected_before_processing(client, db_session):
    """Tope por respuesta en el schema: un texto enorme no llega a `split()`."""
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["a " * 5000]
    )

    assert res.status_code == 422
    # Lo rechaza la validación del schema (detail en lista), no el helper.
    assert isinstance(res.json()["detail"], list)
    assert await _saved_answers(db_session, tenant) == []


@pytest.mark.asyncio
async def test_emoji_with_zero_width_joiner_is_accepted(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=["Soy 👩‍💻"]
    )

    assert res.status_code == 201, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("char", ["\u200b", "\u200e", "\u2066", "\ufeff"])
async def test_invisible_characters_are_rejected(client, db_session, char):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client, "public", raw_key, tenant, service, START, answers=[f"Fiat{char}"]
    )

    assert res.status_code == 422
    assert res.json()["detail"] == "Una respuesta tiene caracteres no permitidos."


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_answers_to_outdated_questions_are_rejected(client, db_session, endpoint):
    """La página manda las preguntas que mostró: si el dueño las cambió en el
    medio, las respuestas no se guardan bajo otra pregunta."""
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client,
        endpoint,
        raw_key,
        tenant,
        service,
        START,
        answers=["Fiat", ""],
        questions=["¿Pregunta vieja?", "¿Alguna indicación?"],
    )

    assert res.status_code == 422
    assert res.json()["detail"] == (
        "Las preguntas de este servicio cambiaron. Recargá la página para verlas."
    )
    assert await _saved_answers(db_session, tenant) == []


@pytest.mark.asyncio
async def test_answers_with_current_questions_are_saved(client, db_session):
    tenant, service, raw_key = await _setup_with_questions(db_session)

    res = await _post_booking(
        client,
        "public",
        raw_key,
        tenant,
        service,
        START,
        answers=["Fiat", ""],
        questions=[q["text"] for q in QUESTIONS],
    )

    assert res.status_code == 201, res.text
