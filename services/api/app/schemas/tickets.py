import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    order_id: uuid.UUID | None
    subject: str
    status: str
    priority: str
    assigned_to: uuid.UUID | None


class TicketListOut(BaseModel):
    items: list[TicketOut]
    total: int
    limit: int
    offset: int


class TicketCreateIn(BaseModel):
    # Clients must not be allowed to submit customer_id or role.
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=3, max_length=200)
    order_id: uuid.UUID | None = None

    @field_validator("subject", mode="before")
    @classmethod
    def strip_subject(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class TicketPatchIn(BaseModel):
    # Only these three fields may be updated through this endpoint.
    model_config = ConfigDict(extra="forbid")

    status: str | None = Field(default=None, min_length=1, max_length=30)
    priority: str | None = Field(default=None, min_length=1, max_length=30)
    assigned_to: uuid.UUID | None = None