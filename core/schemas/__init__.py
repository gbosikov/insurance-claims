"""Pydantic v2 схемы для API и внутреннего обмена данными."""

from core.schemas.claim import (
    ClaimCreate, ClaimResponse, ClaimStatusResponse,
    ExtractionResult, InsuredData, EventData, DiagnoisItem, LineItem,
)
from core.schemas.decision import (
    DiagnosisDecisionSchema, LineItemDecisionSchema, ClaimDecision,
)
from core.schemas.contract import ContractChunkSchema, ContractVersionSchema
from core.schemas.core_api import PolicyInfo, PolicyLimits, ContractMeta

__all__ = [
    "ClaimCreate", "ClaimResponse", "ClaimStatusResponse",
    "ExtractionResult", "InsuredData", "EventData", "DiagnoisItem", "LineItem",
    "DiagnosisDecisionSchema", "LineItemDecisionSchema", "ClaimDecision",
    "ContractChunkSchema", "ContractVersionSchema",
    "PolicyInfo", "PolicyLimits", "ContractMeta",
]
