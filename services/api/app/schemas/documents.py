import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


DocumentVisibility = Literal["public", "internal"]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: str
    visibility: str
    version: int
    created_at: datetime
