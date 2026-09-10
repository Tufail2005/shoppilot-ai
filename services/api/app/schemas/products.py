import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sku: str
    name: str
    description: str | None
    price: Decimal
    stock: int
    category: str | None
    created_at: datetime
    updated_at: datetime


class ProductListOut(BaseModel):
    items: list[ProductOut]
    total: int
    limit: int
    offset: int
