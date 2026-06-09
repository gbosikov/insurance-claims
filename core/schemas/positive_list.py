"""Schemas для POSITIVE LIST процедур."""

from typing import Optional
from pydantic import BaseModel, Field


class PositiveListProcedureSchema(BaseModel):
    """Явно покрытая процедура из контракта."""

    id: str
    procedure_code: Optional[str] = None
    procedure_name_ka: str = Field(..., description="На грузинском")
    procedure_name_ru: Optional[str] = None
    procedure_name_en: Optional[str] = None
    coverage_percent: float = Field(default=100.0)
    sublimit: Optional[float] = None
    section_reference: Optional[str] = None

    class Config:
        from_attributes = True


class PositiveListMatchResult(BaseModel):
    """Результат поиска процедуры в POSITIVE LIST."""

    is_in_positive_list: bool
    procedure: Optional[PositiveListProcedureSchema] = None
    match_type: str = Field(default="none")  # exact | partial | none
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: Optional[str] = None
