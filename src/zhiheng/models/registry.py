"""Public Provider preset registry used by the settings wizard.

Presets describe capabilities and discovery only.  They never contain user
secrets and do not imply that an instance has passed connectivity validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProviderDefinition:
    provider_id: str
    display_name: str
    default_base_url: str
    auth_kind: str
    discovery_kind: str
    protocols: tuple[str, ...]
    capabilities: tuple[str, ...]
    implementation_status: str
    editable_base_url: bool
    documentation_url: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        "openai", "OpenAI", "https://api.openai.com/v1", "bearer", "models",
        ("responses", "chat_completions", "embeddings"), ("text", "multimodal", "embedding"),
        "supported", False, "https://platform.openai.com/docs/models",
    ),
    ProviderDefinition(
        "openai-compatible", "OpenAI-compatible 中转站", "https://api.example.com/v1",
        "bearer", "models",
        ("chat_completions", "embeddings"), ("text", "multimodal", "embedding"),
        "supported", True, "https://platform.openai.com/docs/api-reference",
    ),
    ProviderDefinition(
        "deepseek", "DeepSeek", "https://api.deepseek.com", "bearer", "models",
        ("responses", "chat_completions"), ("text", "multimodal"),
        "supported", False, "https://api-docs.deepseek.com/",
    ),
    ProviderDefinition(
        "anthropic", "Anthropic", "https://api.anthropic.com", "bearer", "unsupported",
        ("anthropic_messages",), ("text", "multimodal"),
        "unsupported", True, "https://docs.anthropic.com/",
    ),
    ProviderDefinition(
        "gemini", "Google Gemini", "https://generativelanguage.googleapis.com", "bearer",
        "unsupported",
        ("google_generate_content",), ("text", "multimodal"),
        "unsupported", True, "https://ai.google.dev/gemini-api/docs",
    ),
    ProviderDefinition(
        "ollama", "Ollama", "http://127.0.0.1:11434", "none", "ollama_tags",
        ("chat_completions",), ("text", "multimodal"),
        "supported", True, "https://ollama.com/library",
    ),
    ProviderDefinition(
        "lm-studio", "LM Studio", "http://127.0.0.1:1234/v1", "bearer", "models",
        ("chat_completions",), ("text", "multimodal"),
        "unsupported", True, "https://lmstudio.ai/docs",
    ),
    ProviderDefinition(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "bearer", "models",
        ("chat_completions",), ("text", "multimodal"),
        "unsupported", True, "https://openrouter.ai/docs",
    ),
    ProviderDefinition(
        "siliconflow", "硅基流动", "https://api.siliconflow.cn/v1", "bearer", "embedding_models",
        ("chat_completions", "embeddings"), ("text", "embedding"),
        "supported", False, "https://docs.siliconflow.cn/docs/api/embeddings-post",
    ),
)

_BY_ID = {item.provider_id: item for item in PROVIDER_DEFINITIONS}


def provider_definition(provider_id: str) -> ProviderDefinition | None:
    return _BY_ID.get(provider_id)


def list_provider_definitions() -> list[dict[str, object]]:
    return [item.as_dict() for item in PROVIDER_DEFINITIONS]
