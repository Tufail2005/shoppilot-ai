import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    status: str
    total: Decimal
    created_at: datetime


class OrderListOut(BaseModel):
    items: list[OrderOut]
    total: int
    limit: int
    offset: int