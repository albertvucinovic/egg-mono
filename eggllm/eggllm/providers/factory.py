"""Adapter factory for selecting the appropriate provider adapter.

The factory pattern allows models to specify which API type they use
(e.g., 'chat_completions' vs 'responses'), enabling a single provider
like OpenAI to support both old and new API styles.
"""

from typing import Dict, Type

from .base import ProviderAdapter
from .openai_compat import OpenAICompatAdapter
from .openai_responses import OpenAIResponsesAdapter


class AdapterFactory:
    """Factory for creating provider adapters based on api_type configuration."""

    # Registry of api_type -> adapter class
    _adapters: Dict[str, Type[ProviderAdapter]] = {
        "chat_completions": OpenAICompatAdapter,
        "responses": OpenAIResponsesAdapter,
    }

    # Singleton instances (adapters are stateless, so we can reuse them)
    _instances: Dict[str, ProviderAdapter] = {}

    @classmethod
    def get_adapter(cls, api_type: str = "chat_completions") -> ProviderAdapter:
        """Get an adapter instance for the given api_type.

        Args:
            api_type: The API type to use. Defaults to 'chat_completions'.
                      Supported values: 'chat_completions', 'responses'

        Returns:
            A ProviderAdapter instance for the requested API type.

        Raises:
            ValueError: If the api_type is not recognized.
        """
        if api_type not in cls._adapters:
            supported = ", ".join(sorted(cls._adapters.keys()))
            raise ValueError(
                f"Unknown api_type: '{api_type}'. Supported types: {supported}"
            )

        # Return cached instance or create new one
        if api_type not in cls._instances:
            cls._instances[api_type] = cls._adapters[api_type]()

        return cls._instances[api_type]

    @classmethod
    def register_adapter(cls, api_type: str, adapter_class: Type[ProviderAdapter]) -> None:
        """Register a new adapter type.

        This allows external code to add new API adapters without modifying
        the factory source.

        Args:
            api_type: The identifier for this adapter type
            adapter_class: The ProviderAdapter subclass to instantiate
        """
        cls._adapters[api_type] = adapter_class
        # Clear any cached instance so the new class is used
        if api_type in cls._instances:
            del cls._instances[api_type]

    @classmethod
    def supported_types(cls) -> list[str]:
        """Return list of supported api_type values."""
        return sorted(cls._adapters.keys())
