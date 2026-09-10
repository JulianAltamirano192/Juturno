from datetime import datetime
from typing import Optional, List, Any
from sqlmodel import SQLModel, Field, Relationship, Column, JSON
from sqlalchemy import UniqueConstraint, CheckConstraint, Index
from sqlalchemy.dialects.postgresql import ExcludeConstraint, TSTZRANGE
from sqlalchemy import text

class Tenant(SQLModel, table=True):
    """
    Representa a un cliente del SaaS (ej. una peluquería o consultorio).
    Es la raíz del aislamiento de datos (Multi-tenant).
    """
    __tablename__ = "tenant"
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Campos de datos
    name: str = Field(index=True)
    whatsapp_number: Optional[str] = None
    timezone: str = Field(default="UTC")
    
    # Relaciones bidireccionales definidas en ambos lados
    services: List["Service"] = Relationship(back_populates="tenant", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    staff_members: List["Staff"] = Relationship(back_populates="tenant", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    bookings: List["Booking"] = Relationship(back_populates="tenant", sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class Service(SQLModel, table=True):
    """
    Define los servicios que se pueden reservar en un Tenant.
    Provee semántica de duración y precio base.
    """
    __tablename__ = "service"
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Foreign Key con CASCADE hacia Tenant y su respectivo index
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    
    # Campos de datos
    name: str
    duration_minutes: int
    price: float
    is_active: bool = Field(default=True, index=True)
    
    # Relaciones bidireccionales definidas en ambos lados
    tenant: Optional[Tenant] = Relationship(back_populates="services")
    bookings: List["Booking"] = Relationship(back_populates="service", sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class Staff(SQLModel, table=True):
    """
    Profesional o recurso físico (silla, consultorio) que atiende el servicio.
    """
    __tablename__ = "staff"
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Foreign Key con CASCADE hacia Tenant
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    
    # Campos de datos
    name: str
    
    # Relaciones bidireccionales
    tenant: Optional[Tenant] = Relationship(back_populates="staff_members")
    bookings: List["Booking"] = Relationship(back_populates="staff")


class Booking(SQLModel, table=True):
    """
    El núcleo del sistema. Intersección de Tenant, Service, Staff y Cliente.
    Implementa snapshots de precio/cliente, Idempotency Key y slot temporal estricto (DateTime).
    """
    __tablename__ = "booking"
    
    __table_args__ = (
        # Índice único anti-race (Idempotencia)
        UniqueConstraint("idempotency_key", name="uq_booking_idempotency_key"),
        
        # Red de seguridad física contra superposición de turnos para el mismo profesional (Exclusion Constraint).
        ExcludeConstraint(
            ('staff_id', '='),
            (text("tstzrange(start_time, end_time)"), '&&'),
            name='excl_overlapping_bookings',
            using='gist'
        ),
    )
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Foreign Keys con CASCADE requeridas y sus índices
    tenant_id: int = Field(foreign_key="tenant.id", index=True, ondelete="CASCADE")
    service_id: int = Field(foreign_key="service.id", index=True, ondelete="CASCADE")
    staff_id: Optional[int] = Field(default=None, foreign_key="staff.id", index=True, ondelete="SET NULL")
    
    # Snapshot de cliente denormalizado
    client_name: str
    client_phone: str
    
    # Campos de slot temporal estrictamente como DateTime
    start_time: datetime = Field(index=True)
    end_time: datetime = Field(index=True)
    
    # Snapshot financiero y de estados
    price_at_booking: float
    status: str = Field(default="pending", index=True) # pending, confirmed, cancelled
    
    # Campo agregado para el control de recordatorios del scheduler (Fase 4)
    reminder_sent: bool = Field(default=False, index=True)
    
    # Clave de idempotencia
    idempotency_key: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Relaciones bidireccionales definidas en ambos lados
    tenant: Optional[Tenant] = Relationship(back_populates="bookings")
    service: Optional[Service] = Relationship(back_populates="bookings")
    staff: Optional[Staff] = Relationship(back_populates="bookings")
    payments: List["Payment"] = Relationship(back_populates="booking", sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class Payment(SQLModel, table=True):
    """
    Traza el historial financiero 1:N por reserva (señas, saldos, reembolsos).
    """
    __tablename__ = "payment"
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Foreign Key con CASCADE hacia Booking
    booking_id: int = Field(foreign_key="booking.id", index=True, ondelete="CASCADE")
    
    # Campos financieros
    amount: float
    method: str
    status: str = Field(index=True)
    paid_at: Optional[datetime] = None
    
    # Relación bidireccional
    booking: Optional[Booking] = Relationship(back_populates="payments")


class NotificationOutbox(SQLModel, table=True):
    """
    Tabla de Cola (Outbox Pattern) para notificaciones asíncronas.
    """
    __tablename__ = "notification_outbox"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True)
    notification_type: str # 'confirmation' o 'reminder'
    status: str = Field(default="pending", index=True) # pending, sent, failed
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProcessedWebhookEvent(SQLModel, table=True):
    """
    Tabla de Idempotencia para Webhooks (Mercado Pago).
    Garantiza el procesamiento 'at-least-once' convirtiéndolo en único.
    """
    __tablename__ = "payment_events"
    
    # event_id es la clave primaria única que previene duplicados
    event_id: str = Field(primary_key=True, index=True)
    booking_id: Optional[int] = Field(default=None, foreign_key="booking.id", index=True, nullable=True)
    event_type: str
    
    # Auditoría del JSON completo recibido de Mercado Pago
    payload: Any = Field(default={}, sa_column=Column(JSON))
    
    received_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = None
    status: str = Field(default="received", index=True) # received, processing, processed, failed