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
    render_command_registry_help,
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


def test_core_lifecycle_commands_are_registered_handlers(tmp_path, monkeypatch) -> None:
    registry = create_default_command_registry()
    help_prints = []

    class App:
        running = True
        _reload_requested = False
        _reload_via_shell = False
        current_thread = "thread-123"
        command_registry = registry

    app = App()
    result = registry.execute(
        "help",
        CommandContext(app=app, log_system=lambda message: None, console_print_block=lambda *args, **kwargs: help_prints.append(args)),
    )
    assert result.clear_input is True
    assert any("/help" in str(call) for call in help_prints)

    result = registry.execute("quit", CommandContext(app=app))
    assert result.exit_app is True
    assert app.running is False

    state_file = tmp_path / "reload-state"
    monkeypatch.setenv("EGG_RELOAD_STATE_FILE", str(state_file))
    result = registry.execute("reload", CommandContext(app=app, current_thread=app.current_thread, log_system=lambda message: None))
    assert result.exit_app is True
    assert app._reload_requested is True
    assert state_file.read_text(encoding="utf-8").strip() == app.current_thread


def test_tools_on_off_commands_are_registered_handlers(monkeypatch) -> None:
    registry = create_default_command_registry()
    calls: list[tuple[str, bool]] = []
    logs: list[str] = []

    monkeypatch.setattr(
        "eggthreads.set_thread_tools_enabled",
        lambda db, tid, enabled: calls.append((tid, enabled)),
    )

    ctx = CommandContext(db=object(), current_thread="thread-1", log_system=logs.append)

    assert registry.execute("toolsOn", ctx).clear_input is True
    assert registry.execute("toolsOff", ctx).clear_input is True

    assert calls == [("thread-1", True), ("thread-1", False)]
    assert any("Tools enabled" in message for message in logs)
    assert any("Tools disabled" in message for message in logs)


def test_enable_disable_tool_commands_are_registered_handlers(monkeypatch) -> None:
    registry = create_default_command_registry()
    disabled: list[tuple[str, str]] = []
    enabled: list[tuple[str, str]] = []
    logs: list[str] = []

    monkeypatch.setattr(
        "eggthreads.disable_tool_for_thread",
        lambda db, tid, name: disabled.append((tid, name)),
    )
    monkeypatch.setattr(
        "eggthreads.enable_tool_for_thread",
        lambda db, tid, name: enabled.append((tid, name)),
    )

    ctx = CommandContext(db=object(), current_thread="thread-1", log_system=logs.append)

    registry.execute("disableTool", ctx, "bash")
    registry.execute("enableTool", ctx, "python")
    result = registry.execute("disableTool", ctx, "")

    assert disabled == [("thread-1", "bash")]
    assert enabled == [("thread-1", "python")]
    assert result.clear_input is False
    assert any("Usage: /disabletool" in message for message in logs)


def test_tools_secrets_status_and_info_commands_are_registered_handlers(monkeypatch) -> None:
    registry = create_default_command_registry()
    raw_values: list[tuple[str, bool]] = []
    logs: list[str] = []
    printed: list[tuple[str, str]] = []

    class Config:
        llm_tools_enabled = True
        allow_raw_tool_output = False
        allowed_tools = {"bash"}
        disabled_tools = set()

    monkeypatch.setattr(
        "eggthreads.set_thread_allow_raw_tool_output",
        lambda db, tid, value: raw_values.append((tid, value)),
    )
    monkeypatch.setattr("eggthreads.get_thread_tools_config", lambda db, tid: Config())
    monkeypatch.setattr(
        "eggthreads.command_catalog._get_available_tools",
        lambda: {
            "bash": {"spec": {"name": "bash"}, "local_only": False},
            "python": {"spec": {"name": "python"}, "local_only": False},
        },
    )

    ctx = CommandContext(
        db=object(),
        current_thread="thread-1",
        log_system=logs.append,
        console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
    )

    registry.execute("toolsSecrets", ctx, "on")
    invalid = registry.execute("toolsSecrets", ctx, "bad")
    registry.execute("toolsStatus", ctx)
    registry.execute("toolInfo", ctx, "BASH")

    assert raw_values == [("thread-1", True)]
    assert invalid.clear_input is False
    assert any("Usage: /toolsSecrets" in message for message in logs)
    assert any(title == "Tools Status" and "python: not allowed" in text for title, text in printed)
    assert any(title == "Tool: bash" and '"name": "bash"' in text for title, text in printed)


def test_toggle_auto_approval_command_is_registered_handler(tmp_path) -> None:
    registry = create_default_command_registry()
    logs: list[str] = []

    from eggthreads import ThreadsDB, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, "auto-approval-test")
    ctx = CommandContext(db=db, current_thread=thread_id, log_system=logs.append)

    registry.execute("toggleAutoApproval", ctx)
    registry.execute("toggleAutoApproval", ctx)

    decisions = []
    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval' ORDER BY event_seq ASC",
        (thread_id,),
    ).fetchall()
    for (payload_json,) in rows:
        import json

        decisions.append(json.loads(payload_json)["decision"])

    assert decisions[-2:] == ["global_approval", "revoke_global_approval"]
    assert any("ENABLED" in message for message in logs)
    assert any("DISABLED" in message for message in logs)


def test_render_command_registry_help_uses_metadata() -> None:
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="example",
            aliases=("ex",),
            category="plugins",
            usage="/example <arg>",
            description="Example command.",
            handler=lambda ctx, arg: None,
        )
    )

    text = render_command_registry_help(registry)

    assert "Plugins:" in text
    assert "/example <arg> (aliases: /ex) — Example command." in text
