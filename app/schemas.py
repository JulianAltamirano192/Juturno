from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class SlotQuery(BaseModel):
    tenant_id: int = Field(gt=0, description="ID del negocio")
    service_id: int = Field(gt=0, description="ID del servicio requerido")
    day: date = Field(description="Fecha a consultar YYYY-MM-DD")
    staff_id: int | None = Field(default=None, description="ID del profesional")

    @field_validator("day", mode="before")
    @classmethod
    def not_in_past(cls, v):
        d = date.fromisoformat(v) if isinstance(v, str) else v
        if d < date.today():
            raise ValueError("No se pueden consultar fechas pasadas")
        return d


class AvailableSlotsResponse(BaseModel):
    date: date
    service_duration_min: int
    timezone: str
    slots: list[str]


class BookingCreate(BaseModel):
    tenant_id: int
    service_id: int
    staff_id: int | None = None
    client_name: str
    client_phone: str
    start_time: datetime
    end_time: datetime | None = None
    idempotency_key: str
