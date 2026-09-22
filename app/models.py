from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Any
from sqlmodel import SQLModel, Field, Relationship, Column, JSON
from sqlalchemy import DateTime, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy import text


class Tenant(SQLModel, table=True):
    """
    Representa a un cliente del SaaS (ej. una peluquería o consultorio).
    Es la raíz del aislamiento de datos (Multi-tenant).
    """

    __tablename__ = "tenant"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    slug: Optional[str] = Field(default=None, index=True, unique=True)
    whatsapp_number: Optional[str] = None
    timezone: str = Field(default="UTC")

    services: List["Service"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    staff_members: List["Staff"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    bookings: List["Booking"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    api_keys: List["ApiKey"] = Relationship(
        back_populates="tenant",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Service(SQLModel, table=True):
    """
    Define los servicios que se pueden reservar en un Tenant.
    """

    __tablename__ = "service"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    name: str
    duration_minutes: int
    price: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    is_active: bool = Field(default=True, index=True)

    tenant: Optional[Tenant] = Relationship(back_populates="services")
    bookings: List["Booking"] = Relationship(
        back_populates="service",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Staff(SQLModel, table=True):
    """
    Profesional o recurso físico que atiende el servicio.
    """

    __tablename__ = "staff"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    name: str

    tenant: Optional[Tenant] = Relationship(back_populates="staff_members")
    bookings: List["Booking"] = Relationship(back_populates="staff")


class Booking(SQLModel, table=True):
    """
    El núcleo del sistema. Intersección de Tenant, Service, Staff y Cliente.
    """

    __tablename__ = "booking"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_booking_idempotency_key"),
        ExcludeConstraint(
            (text("tenant_id"), "="),
            (text("(COALESCE(staff_id, -1))"), "="),
            (text("tstzrange(start_time, end_time)"), "&&"),
            name="excl_overlapping_bookings",
            using="gist",
            where=text("status IN ('pending', 'confirmed')"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    service_id: int = Field(foreign_key="service.id", index=True, ondelete="CASCADE")
    staff_id: Optional[int] = Field(
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

    idempotency_key: str = Field(index=True, unique=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=text("NOW()"),
        ),
    )

    tenant: Optional[Tenant] = Relationship(back_populates="bookings")
    service: Optional[Service] = Relationship(back_populates="bookings")
    staff: Optional[Staff] = Relationship(back_populates="bookings")
    payments: List["Payment"] = Relationship(
        back_populates="booking",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Payment(SQLModel, table=True):
    """
    Traza el historial financiero 1:N por reserva.
    """

    __tablename__ = "payment"

    id: Optional[int] = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True, ondelete="CASCADE")
    amount: Decimal = Field(sa_column=Column(Numeric(10, 2), nullable=False))
    mp_payment_id: Optional[str] = Field(default=None, index=True)
    method: str
    status: str = Field(index=True)
    paid_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    booking: Optional[Booking] = Relationship(back_populates="payments")


class NotificationOutbox(SQLModel, table=True):
    """
    Tabla de Cola (Outbox Pattern) para notificaciones asíncronas.
    """

    __tablename__ = "notification_outbox"

    id: Optional[int] = Field(default=None, primary_key=True)
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

    error_message: Optional[str] = None

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
    booking_id: Optional[int] = Field(
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

    processed_at: Optional[datetime] = Field(
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

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    key_hash: str = Field(index=True, unique=True)
    label: Optional[str] = None

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    last_used_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    tenant: Optional[Tenant] = Relationship(back_populates="api_keys")
