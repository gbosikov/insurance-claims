"""Фабрика Core System Adapter."""

from core.config import get_settings
from layers.core_adapter.interface import CoreSystemAdapter
from layers.core_adapter.rest_adapter import LiteGroupAdapter, MockCoreAdapter

settings = get_settings()


def get_core_adapter() -> CoreSystemAdapter:
    """
    Возвращает нужный адаптер:
    - MockCoreAdapter если CORE_API_BASE_URL=http://mock-core (dev)
    - LiteGroupAdapter для реального подключения к Lite GROUP
    """
    if "mock" in settings.core_api_base_url:
        return MockCoreAdapter()
    return LiteGroupAdapter()
