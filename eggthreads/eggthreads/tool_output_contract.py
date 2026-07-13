from __future__ import annotations

"""Central publication contracts for bounded recovery/meta tools."""

from dataclasses import dataclass

from .terminal_safety import sanitize_terminal_text


HARD_BYPASS_TOOL_NAMES = frozenset({"read_long_tool_output", "extract_tool_output"})
HARD_BYPASS_MAX_CANONICAL_CHARS = 60_000
HARD_BYPASS_MAX_PUBLISHED_CHARS = 70_000


@dataclass(frozen=True)
class ToolOutputContract:
    bypass_optimizer: bool = False
    bypass_long_output_routing: bool = False
    max_canonical_chars: int | None = None
    max_published_chars: int | None = None


_DEFAULT_CONTRACT = ToolOutputContract()
_BOUNDED_BYPASS_CONTRACT = ToolOutputContract(
    bypass_optimizer=True,
    bypass_long_output_routing=True,
    max_canonical_chars=HARD_BYPASS_MAX_CANONICAL_CHARS,
    max_published_chars=HARD_BYPASS_MAX_PUBLISHED_CHARS,
)


def tool_output_contract(tool_name: str) -> ToolOutputContract:
    if str(tool_name or "").strip() in HARD_BYPASS_TOOL_NAMES:
        return _BOUNDED_BYPASS_CONTRACT
    return _DEFAULT_CONTRACT


def is_hard_bypass_tool(tool_name: str) -> bool:
    contract = tool_output_contract(tool_name)
    return contract.bypass_optimizer and contract.bypass_long_output_routing


def canonical_terminal_safe_output(output: str) -> str:
    """Return the one canonical text used before every publication transform."""

    if not isinstance(output, str):
        output = str(output or "")
    return sanitize_terminal_text(output)


def bounded_bypass_publication(tool_name: str, canonical: str, published: str) -> tuple[str, bool]:
    """Return a bounded safe publication, replacing producer violations briefly."""

    contract = tool_output_contract(tool_name)
    safe_canonical = canonical_terminal_safe_output(canonical)
    safe_published = sanitize_terminal_text(published)
    violated = (
        canonical != safe_canonical
        or published != safe_published
        or (
            contract.max_canonical_chars is not None
            and len(safe_canonical) > contract.max_canonical_chars
        )
        or (
            contract.max_published_chars is not None
            and len(safe_published) > contract.max_published_chars
        )
    )
    if violated:
        return f"Error: {tool_name} violated its bounded safe-output contract.", True
    return safe_published, False


def validate_bounded_tool_output(tool_name: str, canonical: str, published: str) -> None:
    """Verify either the exact safe result or the exact brief violation receipt."""

    contract = tool_output_contract(tool_name)
    if not contract.bypass_long_output_routing:
        return
    expected, _violated = bounded_bypass_publication(tool_name, canonical, published)
    if expected != published:
        raise ValueError(f"{tool_name} output violates its bounded safe-output contract")


def requires_legacy_long_output_routing(tool_name: str) -> bool:
    """Whether the TC5 compatibility safety net may create a long artifact."""

    return not tool_output_contract(tool_name).bypass_long_output_routing


__all__ = [
    "HARD_BYPASS_MAX_CANONICAL_CHARS",
    "HARD_BYPASS_MAX_PUBLISHED_CHARS",
    "HARD_BYPASS_TOOL_NAMES",
    "ToolOutputContract",
    "bounded_bypass_publication",
    "canonical_terminal_safe_output",
    "is_hard_bypass_tool",
    "requires_legacy_long_output_routing",
    "tool_output_contract",
    "validate_bounded_tool_output",
]
