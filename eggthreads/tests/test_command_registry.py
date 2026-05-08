from __future__ import annotations

import asyncio

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


def test_default_registry_uses_tools_admin_plugin_handlers() -> None:
    from eggthreads.builtin_plugins import tools_admin

    registry = create_default_command_registry()

    assert registry.get("toolsOn").handler is tools_admin.tools_on_command
    assert registry.get("toolsOff").handler is tools_admin.tools_off_command
    assert registry.get("disableTool").handler is tools_admin.disable_tool_command
    assert registry.get("enableTool").handler is tools_admin.enable_tool_command
    assert registry.get("toolsStatus").handler is tools_admin.tools_status_command
    assert registry.get("toolInfo").handler is tools_admin.tool_info_command
    assert registry.get("toolsSecrets").handler is tools_admin.tools_secrets_command
    assert registry.get("toggleAutoApproval").handler is tools_admin.toggle_auto_approval_command


def test_tools_on_off_commands_are_registered_handlers(monkeypatch) -> None:
    registry = create_default_command_registry()
    calls: list[tuple[str, bool]] = []
    logs: list[str] = []

    monkeypatch.setattr(
        "eggthreads.builtin_plugins.tools_admin.set_thread_tools_enabled",
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
        "eggthreads.builtin_plugins.tools_admin.disable_tool_for_thread",
        lambda db, tid, name: disabled.append((tid, name)),
    )
    monkeypatch.setattr(
        "eggthreads.builtin_plugins.tools_admin.enable_tool_for_thread",
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
        "eggthreads.builtin_plugins.tools_admin.set_thread_allow_raw_tool_output",
        lambda db, tid, value: raw_values.append((tid, value)),
    )
    monkeypatch.setattr("eggthreads.builtin_plugins.tools_admin.get_thread_tools_config", lambda db, tid: Config())
    monkeypatch.setattr(
        "eggthreads.builtin_plugins.tools_admin.available_tools",
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


def test_thread_ui_commands_are_registered_handlers(tmp_path, monkeypatch) -> None:
    from eggthreads.builtin_plugins import thread_ui
    from eggthreads import ThreadsDB, append_message, create_child_thread, create_root_thread, create_snapshot

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    root = create_root_thread(db, "root")
    append_message(db, root, "system", "sys")
    create_snapshot(db, root)
    child = create_child_thread(db, root, "child")
    create_snapshot(db, child)

    current = {"thread_id": root}
    logs: list[str] = []
    printed: list[tuple[str, str]] = []
    started: list[str] = []

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: type("Loop", (), {"create_task": lambda self, coro: None})())

    def make_context() -> CommandContext:
        return CommandContext(
            db=db,
            current_thread=current["thread_id"],
            set_current_thread=lambda tid: current.__setitem__("thread_id", tid),
            log_system=logs.append,
            console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
            start_scheduler=started.append,
            system_prompt="sys",
            get_current_model=lambda tid: None,
            watch_current_thread=lambda: None,
            print_current_thread=lambda **kwargs: None,
            format_threads=lambda root_tid=None: f"tree:{root_tid or 'all'}",
            select_threads=lambda selector: [row[0] for row in db.conn.execute("SELECT thread_id FROM threads") if row[0].endswith(selector) or selector in row[0]],
        )

    registry = create_default_command_registry()

    assert registry.get("thread").handler is thread_ui.thread_command
    assert registry.get("threads").handler is thread_ui.threads_command
    assert registry.get("newThread").handler is thread_ui.new_thread_command
    assert registry.get("deleteThread").handler is thread_ui.delete_thread_command
    assert registry.get("duplicateThread").handler is thread_ui.duplicate_thread_command
    assert registry.get("parentThread").handler is thread_ui.parent_thread_command
    assert registry.get("listChildren").handler is thread_ui.list_children_command
    assert registry.get("continue").handler is thread_ui.continue_thread_command

    registry.execute("thread", make_context(), child[-8:])
    assert current["thread_id"] == child

    registry.execute("parentThread", make_context())
    assert current["thread_id"] == root

    registry.execute("listChildren", make_context())
    registry.execute("threads", make_context())
    assert ("Subtree", f"tree:{root}") in printed
    assert ("Threads", "tree:all") in printed

    registry.execute("newThread", make_context(), "created")
    assert current["thread_id"] != root
    assert db.get_thread(current["thread_id"]) is not None

    current["thread_id"] = root
    registry.execute("duplicateThread", make_context(), "copy")
    duplicate_id = current["thread_id"]
    assert duplicate_id not in {root, child}
    assert db.get_thread(duplicate_id) is not None

    current["thread_id"] = root
    registry.execute("deleteThread", make_context(), duplicate_id[-8:])
    assert db.get_thread(duplicate_id) is None
    assert any("Switched to thread" in message for message in logs)


def test_subagent_commands_are_registered_handlers(tmp_path, monkeypatch) -> None:
    from eggthreads import ThreadsDB, create_child_thread, create_root_thread, create_snapshot

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    root = create_root_thread(db, "root")
    child = create_child_thread(db, root, "child")
    create_snapshot(db, child)

    tool_calls: list[tuple[str, dict]] = []

    class Tools:
        def execute(self, name, args):
            tool_calls.append((name, args))
            return "spawned-child-id"

    monkeypatch.setattr("eggthreads.tools.create_default_tools", lambda: Tools())

    messages: list[tuple[str, str, dict | None]] = []
    approvals: list[tuple[str, str, str | None]] = []
    snapshots: list[str] = []
    started: list[str] = []
    logs: list[str] = []

    ctx = CommandContext(
        db=db,
        current_thread=root,
        log_system=logs.append,
        start_scheduler=started.append,
        system_prompt="sys",
        append_message=lambda db, tid, role, content, extra=None: messages.append((role, content, extra)) or "msg-1",
        create_snapshot=lambda db, tid: snapshots.append(tid),
        approve_tool_calls=lambda db, tid, decision, reason=None, tool_call_id=None: approvals.append((tid, decision, tool_call_id)),
        select_threads=lambda selector: [tid for tid in (root, child) if tid.endswith(selector) or selector in tid],
    )

    registry = create_default_command_registry()

    registry.execute("spawnChildThread", ctx, "do task")
    registry.execute("spawnAutoApprovedChildThread", ctx, "auto task")
    registry.execute("waitForThreads", ctx, child[-8:])

    assert tool_calls[0] == (
        "spawn_agent",
        {
            "parent_thread_id": root,
            "context_text": "do task",
            "label": "spawn",
            "system_prompt": "sys",
        },
    )
    assert tool_calls[1][0] == "spawn_agent_auto"
    assert "spawned-child-id" in started
    assert messages[-1][0] == "user"
    assert messages[-1][2]["tool_calls"][0]["function"]["name"] == "wait"
    assert approvals == [(root, "granted", messages[-1][2]["tool_calls"][0]["id"])]
    assert root in snapshots


def test_sandbox_admin_commands_are_registered_handlers(monkeypatch) -> None:
    from eggthreads.builtin_plugins import sandbox_admin

    registry = create_default_command_registry()

    assert registry.get("toggleSandboxing").handler is sandbox_admin.toggle_sandboxing_command
    assert registry.get("setSandboxConfiguration").handler is sandbox_admin.set_sandbox_configuration_command
    assert registry.get("getSandboxingConfig").handler is sandbox_admin.get_sandboxing_config_command

    logs: list[str] = []
    printed: list[tuple[str, str]] = []
    set_calls: list[dict] = []

    class Config:
        settings = {"provider": "memory"}

    monkeypatch.setattr("eggthreads.is_user_sandbox_control_enabled", lambda db, tid: True)
    monkeypatch.setattr("eggthreads.get_thread_sandbox_status", lambda db, tid: {"enabled": False, "effective": True, "provider": "docker"})
    monkeypatch.setattr("eggthreads.get_thread_sandbox_config", lambda db, tid: Config())
    monkeypatch.setattr("eggthreads.set_thread_sandbox_config", lambda db, tid, **kwargs: set_calls.append(kwargs))

    ctx = CommandContext(
        db=object(),
        current_thread="thread-1",
        log_system=logs.append,
        console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
    )

    registry.execute("toggleSandboxing", ctx)
    registry.execute("setSandboxConfiguration", ctx, "")
    registry.execute("setSandboxConfiguration", ctx, "locked.json")
    registry.execute("getSandboxingConfig", ctx)

    assert set_calls[0]["enabled"] is True
    assert set_calls[0]["settings"] == {"provider": "memory"}
    assert set_calls[1]["config_name"] == "locked.json"
    assert any(title == "Sandbox Configuration" and "Sandbox Configuration and Control" in text for title, text in printed)
    assert any("Sandbox configuration applied" in message for message in logs)


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
