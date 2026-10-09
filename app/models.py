from datetime import datetime, timezone
from datetime import time as time_type
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Time as SATime,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlmodel import JSON, Column, Field, Relationship, SQLModel


class Tenant(SQLModel, table=True):
    """
    Representa a un cliente del SaaS (ej. una peluquería o consultorio).
    Es la raíz del aislamiento de datos (Multi-tenant).
    """

    __tablename__ = "tenant"
    __table_args__ = (
        # Una cuenta de MP pertenece a un solo negocio: el webhook resuelve el
        # tenant por mp_user_id. Parcial para que convivan los NULL.
        Index(
            "uq_tenant_mp_user_id",
            "mp_user_id",
            unique=True,
            postgresql_where=text("mp_user_id IS NOT NULL"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    slug: str | None = Field(default=None, index=True, unique=True)
    whatsapp_number: str | None = None
    timezone: str = Field(default="America/Argentina/Buenos_Aires")
    deposit_expiration_minutes: int | None = Field(
        default=15,
        sa_column=Column(Integer(), nullable=True),
        description=(
            "Minutos que tiene el cliente para pagar la seña antes de que "
            "la reserva expire y libere el horario. NULL = sin expiración."
        ),
    )
    owner_email: str | None = Field(
        default=None,
        index=True,
        unique=True,
        description="Email del dueño del negocio para login en el panel.",
    )
    password_hash: str | None = Field(
        default=None,
        description="Hash PBKDF2-HMAC-SHA256 de la contraseña del dueño.",
    )
    session_version: int = Field(
        default=1,
        sa_column_kwargs={"server_default": text("1")},
        description="Versión de sesión para invalidar todas las cookies activas al cambiar clave.",
    )
    # Credenciales de Mercado Pago conectadas vía OAuth (D-012).
    # Los tokens se guardan CIFRADOS con Fernet (app/mp_crypto.py) —
    # ningún token sensible se persiste en texto plano.
    mp_user_id: str | None = Field(
        default=None,
        description="ID de la cuenta de MP conectada (collector_id).",
    )
    mp_public_key: str | None = Field(
        default=None,
        description="Public key de la cuenta de MP (uso futuro p/ checkout).",
    )
    mp_alias: str | None = Field(
        default=None,
        description="Alias de la cuenta de MP, informativo (de /users/me).",
    )
    mp_access_token_enc: str | None = Field(
        default=None,
        description="Access token OAuth de MP, cifrado con Fernet.",
    )
    mp_refresh_token_enc: str | None = Field(
        default=None,
        description="Refresh token OAuth de MP, cifrado con Fernet.",
    )
    mp_token_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Cuándo vence el access token (MP: ~180 días).",
    )

    services: list["Service"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    staff_members: list["Staff"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    bookings: list["Booking"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    api_keys: list["ApiKey"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Service(SQLModel, table=True):
    """
    Define los servicios que se pueden reservar en un Tenant.
    """

    __tablename__ = "service"
    __table_args__ = (
        CheckConstraint(
            "deposit_amount IS NULL OR deposit_amount >= 0",
            name="ck_service_deposit_amount_non_negative",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    name: str
    duration_minutes: int
    price: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    deposit_amount: Decimal | None = Field(
        default=None,
        sa_column=Column(Numeric(10, 2), nullable=True),
        description="Monto de seña. Si es None, se usa el 30% del precio total.",
    )
    is_active: bool = Field(default=True, index=True)

    tenant: Tenant | None = Relationship(back_populates="services")
    bookings: list["Booking"] = Relationship(
        back_populates="service",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Staff(SQLModel, table=True):
    """
    Profesional o recurso físico que atiende el servicio.
    """

    __tablename__ = "staff"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    name: str
    is_active: bool = Field(default=True, index=True)

    tenant: Tenant | None = Relationship(back_populates="staff_members")
    bookings: list["Booking"] = Relationship(back_populates="staff")


class BusinessHours(SQLModel, table=True):
    """
    Define los horarios de atención de un negocio o de un profesional específico.

    - staff_id = NULL → horario general del negocio (aplica a todos los días
      que no tengan una fila de staff específica).
    - staff_id = <id>  → horario de ese profesional puntual (prioridad sobre
      el horario del negocio).

    Un negocio sin NINGUNA fila en esta tabla → el endpoint cae al fallback
    estático 09-18 para no romper la página pública.
    Un negocio CON filas → un día sin filas = "cerrado ese día".
    """

    __tablename__ = "business_hours"

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "staff_id",
            "day_of_week",
            name="uq_business_hours_tenant_staff_day",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    staff_id: int | None = Field(
        default=None,
        foreign_key="staff.id",
        index=True,
        nullable=True,
        ondelete="CASCADE",
        description="NULL = horario del negocio; valor = horario de ese profesional.",
    )
    day_of_week: int = Field(
        description="0 = lunes … 6 = domingo (Python weekday()).",
        ge=0,
        le=6,
    )
    # Guardamos hora de inicio y fin como Time en Postgres.
    start_time: "time_type" = Field(
        sa_column=Column("start_time", SATime, nullable=False),
        description="Hora de apertura (ej. 09:00).",
    )
    end_time: "time_type" = Field(
        sa_column=Column("end_time", SATime, nullable=False),
        description="Hora de cierre (ej. 18:00).",
    )


class Booking(SQLModel, table=True):
    """
    El núcleo del sistema. Intersección de Tenant, Service, Staff y Cliente.
    """

    __tablename__ = "booking"

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_booking_idempotency_key"
        ),
        ExcludeConstraint(
            (text("tenant_id"), "="),
            (text("(COALESCE(staff_id, -1))"), "="),
            (text("tstzrange(start_time, end_time)"), "&&"),
            name="excl_overlapping_bookings",
            using="gist",
            where=text("status IN ('pending', 'confirmed')"),
        ),
        CheckConstraint(
            "deposit_at_booking IS NULL OR deposit_at_booking >= 0",
            name="ck_booking_deposit_at_booking_non_negative",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    service_id: int = Field(foreign_key="service.id", index=True, ondelete="CASCADE")
    staff_id: int | None = Field(
        default=None, foreign_key="staff.id", index=True, ondelete="SET NULL"
    )

    client_name: str
    client_phone: str

    start_time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    end_time: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )

    price_at_booking: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    deposit_at_booking: Decimal | None = Field(
        default=None,
        sa_column=Column(Numeric(10, 2), nullable=True),
        description="Snapshot del monto de seña exigido al crear la reserva. Inmutable.",
    )

    status: str = Field(
        default="pending",
        index=True,
        sa_column_kwargs={"server_default": text("'pending'")},
    )

    reminder_sent: bool = Field(
        default=False,
        index=True,
        sa_column_kwargs={"server_default": text("false")},
    )

    idempotency_key: str

    # --- Auditoría de acciones (Tarea 8) ---
    status_changed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    status_changed_by: str | None = Field(default=None)
    cancellation_reason: str | None = Field(default=None)
    no_show_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("NOW()"),
        ),
    )

    tenant: Tenant | None = Relationship(back_populates="bookings")
    service: Service | None = Relationship(back_populates="bookings")
    staff: Staff | None = Relationship(back_populates="bookings")
    payments: list["Payment"] = Relationship(
        back_populates="booking",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Payment(SQLModel, table=True):
    """
    Traza el historial financiero 1:N por reserva.
    """

    __tablename__ = "payment"
    __table_args__ = (
        UniqueConstraint("mp_payment_id", name="uq_payment_mp_payment_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True, ondelete="CASCADE")
    amount: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    mp_payment_id: str | None = Field(default=None, index=True)
    mp_preference_id: str | None = Field(default=None, index=True)
    mp_checkout_url: str | None = Field(default=None)
    # Vencimiento del link de pago en MP; después no se puede pagar.
    mp_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    method: str
    status: str = Field(index=True)
    paid_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    booking: Booking | None = Relationship(back_populates="payments")


class NotificationOutbox(SQLModel, table=True):
    """
    Tabla de Cola (Outbox Pattern) para notificaciones asíncronas.
    """

    __tablename__ = "notification_outbox"

    id: int | None = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True, ondelete="CASCADE")
    notification_type: str

    status: str = Field(
        default="pending",
        index=True,
        sa_column_kwargs={"server_default": text("'pending'")},
    )

    retry_count: int = Field(
        default=0,
        sa_column_kwargs={"server_default": text("0")},
    )

    error_message: str | None = None

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("NOW()"),
        ),
    )


class ProcessedWebhookEvent(SQLModel, table=True):
    """
    Tabla de Idempotencia para Webhooks (Mercado Pago).
    """

    __tablename__ = "payment_events"

    event_id: str = Field(primary_key=True, index=True)
    booking_id: int | None = Field(
        default=None,
        foreign_key="booking.id",
        index=True,
        nullable=True,
        ondelete="CASCADE",
    )
    event_type: str

    payload: Any = Field(default={}, sa_column=Column(JSON))

    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("NOW()"),
        ),
    )

    processed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    status: str = Field(
        default="received",
        index=True,
        sa_column_kwargs={"server_default": text("'received'")},
    )


class ApiKey(SQLModel, table=True):
    """
    Credencial de autenticación por tenant (header X-Tenant-API-Key).
    """

    __tablename__ = "api_key"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    key_hash: str = Field(index=True, unique=True)
    label: str | None = None

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    tenant: Tenant | None = Relationship(back_populates="api_keys")
