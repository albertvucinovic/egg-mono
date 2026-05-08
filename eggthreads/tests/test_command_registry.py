from __future__ import annotations

import pytest

from eggthreads.command_catalog import (
    CommandContext,
    CommandRegistry,
    CommandResult,
    CommandSpec,
    InputPrefixRegistry,
    InputPrefixSpec,
    command_completion_names,
    create_default_command_registry,
    create_default_input_prefix_registry,
)


def test_command_registry_executes_registered_command() -> None:
    seen: dict[str, object] = {}
    registry = CommandRegistry()

    def handler(ctx: CommandContext, arg: str) -> CommandResult:
        seen["ctx"] = ctx
        seen["arg"] = arg
        return CommandResult(clear_input=False, message="done")

    registry.register(
        CommandSpec(
            name="example",
            aliases=("ex",),
            category="test",
            usage="/example <arg>",
            description="Example command",
            handler=handler,
        )
    )

    ctx = CommandContext(current_thread="thread-1")
    result = registry.execute("/ex", ctx, "value")

    assert result == CommandResult(clear_input=False, message="done")
    assert seen == {"ctx": ctx, "arg": "value"}
    assert registry.get("example").usage == "/example <arg>"
    assert registry.names(include_aliases=True) == ["example", "ex"]


def test_command_registry_rejects_duplicate_names_and_aliases() -> None:
    registry = CommandRegistry()
    registry.register(CommandSpec(name="example", aliases=("ex",), handler=lambda ctx, arg: None))

    with pytest.raises(ValueError):
        registry.register(CommandSpec(name="example", handler=lambda ctx, arg: None))

    with pytest.raises(ValueError):
        registry.register(CommandSpec(name="other", aliases=("ex",), handler=lambda ctx, arg: None))


def test_default_command_registry_contains_existing_ui_commands() -> None:
    registry = create_default_command_registry()
    names = registry.names()

    for name in [
        "help",
        "quit",
        "reload",
        "toolsStatus",
        "thread",
        "spawnChildThread",
        "sessionStatus",
        "skill",
        "startSearxng",
        "displayMode",
        "authStatus",
    ]:
        assert name in names

    assert "/help" in command_completion_names(registry)
    assert "/sessionStatus" in command_completion_names(registry)


def test_command_registry_uses_completion_callback() -> None:
    registry = CommandRegistry()

    registry.register(
        CommandSpec(
            name="example",
            handler=lambda ctx, arg: None,
            complete=lambda ctx, arg: ["alpha", {"display": "Beta", "insert": "beta"}],
        )
    )

    assert registry.complete("example", CommandContext(), "a") == [
        "alpha",
        {"display": "Beta", "insert": "beta"},
    ]


def test_input_prefix_registry_uses_longest_prefix_match() -> None:
    registry = InputPrefixRegistry()
    seen: list[tuple[str, str]] = []
    registry.register(InputPrefixSpec("$", lambda ctx, arg: seen.append(("visible", arg))))
    registry.register(InputPrefixSpec("$$", lambda ctx, arg: seen.append(("hidden", arg))))

    result = registry.execute("$$ echo secret", CommandContext())

    assert result == CommandResult(clear_input=True)
    assert seen == [("hidden", " echo secret")]


def test_default_input_prefix_registry_dispatches_to_bash_enqueue() -> None:
    calls: list[tuple[str, bool]] = []

    class App:
        def enqueue_bash_tool(self, script: str, hidden: bool) -> None:
            calls.append((script, hidden))

    registry = create_default_input_prefix_registry()
    result = registry.execute("$$ echo secret", CommandContext(app=App()))

    assert result == CommandResult(clear_input=True)
    assert calls == [("echo secret", True)]
