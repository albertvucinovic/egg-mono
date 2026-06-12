from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

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
    assert seen["arg"] == "value"
    assert getattr(seen["ctx"], "current_thread") == ctx.current_thread
    assert registry.get("example").usage == "/example <arg>"
    assert registry.names(include_aliases=True) == ["example", "ex"]


def test_command_registry_returns_logged_message_when_handler_omits_message() -> None:
    logs: list[str] = []
    registry = CommandRegistry()

    def handler(ctx: CommandContext, arg: str) -> CommandResult:
        assert ctx.log_system is not None
        ctx.log_system("handler logged reply")
        return CommandResult(clear_input=True)

    registry.register(CommandSpec(name="example", handler=handler))

    result = registry.execute("example", CommandContext(log_system=logs.append))

    assert result.clear_input is True
    assert result.message == "handler logged reply"
    assert logs == ["handler logged reply"]


def test_command_registry_returns_fallback_message_when_handler_is_silent() -> None:
    registry = CommandRegistry()
    registry.register(CommandSpec(name="example", handler=lambda ctx, arg: CommandResult(clear_input=True)))

    result = registry.execute("example", CommandContext())

    assert result == CommandResult(clear_input=True, message="/example completed.")


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


def test_plugins_expose_common_provider_policy_context_fields() -> None:
    from eggthreads.plugins import CommandPluginContext, ProviderPluginContext, ToolPluginContext

    assert hasattr(ToolPluginContext(tool_registry=object()), "sandbox_provider_registry")
    assert hasattr(CommandPluginContext(command_registry=object()), "output_policy_registry")
    ctx = ProviderPluginContext(
        sandbox_provider_registry="sandbox",
        session_provider_registry="session",
        approval_policy_registry="approval",
        output_policy_registry="output",
    )
    assert ctx.sandbox_provider_registry == "sandbox"
    assert ctx.session_provider_registry == "session"
    assert ctx.approval_policy_registry == "approval"
    assert ctx.output_policy_registry == "output"


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
    result = registry.execute("reload", CommandContext(app=app, db=None, current_thread=app.current_thread, log_system=lambda message: None))
    assert result.exit_app is True
    assert app._reload_requested is True
    assert state_file.read_text(encoding="utf-8").strip() == app.current_thread


def test_reload_skips_when_current_thread_streaming(tmp_path, monkeypatch) -> None:
    from eggthreads import ThreadsDB

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = db.create_thread("thread-streaming", name="root")
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    assert db.try_open_stream(tid, "invoke-current", lease_until, owner="test", purpose="tool")

    class App:
        running = True
        _reload_requested = False
        _reload_via_shell = False
        current_thread = tid

    state_file = tmp_path / "reload-state"
    logs: list[str] = []
    monkeypatch.setenv("EGG_RELOAD_STATE_FILE", str(state_file))

    result = create_default_command_registry().execute(
        "reload",
        CommandContext(app=App(), db=db, current_thread=tid, log_system=logs.append),
    )

    assert result.exit_app is False
    assert App._reload_requested is False
    assert not state_file.exists()
    assert "streaming" in result.message


def test_reload_skips_when_subthread_streaming(tmp_path, monkeypatch) -> None:
    from eggthreads import ThreadsDB

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    root = db.create_thread("thread-root", name="root")
    child = db.create_thread("thread-child", name="child", parent_id=root)
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    assert db.try_open_stream(child, "invoke-child", lease_until, owner="test", purpose="assistant_stream")

    class App:
        running = True
        _reload_requested = False
        _reload_via_shell = False
        current_thread = root

    state_file = tmp_path / "reload-state"
    monkeypatch.setenv("EGG_RELOAD_STATE_FILE", str(state_file))

    result = create_default_command_registry().execute(
        "reload",
        CommandContext(app=App(), db=db, current_thread=root, log_system=lambda message: None),
    )

    assert result.exit_app is False
    assert App._reload_requested is False
    assert not state_file.exists()


def test_model_auth_commands_are_registered_handlers() -> None:
    from eggthreads.builtin_plugins import auth, model

    registry = create_default_command_registry()

    assert registry.get("model").handler is model.model_command
    assert registry.get("updateAllModels").handler is model.update_all_models_command
    assert registry.get("login").handler is auth.login_command
    assert registry.get("logout").handler is auth.logout_command
    assert registry.get("authStatus").handler is auth.auth_status_command


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


def test_default_registry_uses_recovery_toggle_handler() -> None:
    from eggthreads.builtin_plugins import diagnostics

    registry = create_default_command_registry()

    assert registry.get("toggleAutoContinueOnError").handler is diagnostics.toggle_auto_continue_on_error_command


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


def test_diagnostics_commands_are_registered_handlers(monkeypatch) -> None:
    from eggthreads.builtin_plugins import diagnostics

    registry = create_default_command_registry()

    assert registry.get("schedulers").handler is diagnostics.schedulers_command
    assert registry.get("cost").handler is diagnostics.cost_command
    assert registry.get("setContextLimit").handler is diagnostics.set_context_limit_command
    assert registry.get("setThreadPriority").handler is diagnostics.set_thread_priority_command

    logs: list[str] = []
    printed: list[tuple[str, str]] = []

    ctx = CommandContext(
        db=object(),
        current_thread="thread-1",
        log_system=logs.append,
        console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
        app=type("App", (), {"active_schedulers": {}, "current_thread": "thread-1"})(),
    )

    def fake_thread_token_stats(db, thread_id, llm=None):
        return {
            "context_tokens": 10,
            "full_thread_tokens": 30,
            "api_usage": {
                "total_input_tokens": 5,
                "cached_input_tokens": 1,
                "cache_creation_input_tokens": 2,
                "total_output_tokens": 2,
                "approx_call_count": 1,
                "actual_call_count": 1,
                "estimated_call_count": 0,
                "api_confirmed_usage": {
                    "actual_call_count": 1,
                    "total_input_tokens": 5,
                    "cached_input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "total_output_tokens": 2,
                    "field_call_counts": {
                        "total_input_tokens": 1,
                        "cached_input_tokens": 1,
                        "cache_creation_input_tokens": 1,
                        "total_output_tokens": 1,
                    },
                },
                "by_model": {
                    "test-model": {
                        "total_input_tokens": 5,
                        "cached_input_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "total_output_tokens": 2,
                        "approx_call_count": 1,
                        "actual_call_count": 1,
                        "estimated_call_count": 0,
                    }
                },
                "cost_usd": {
                    "total": 0.10,
                    "by_model": {
                        "test-model": {
                            "input": 0.01,
                            "cached": 0.02,
                            "cache_creation": 0.03,
                            "output": 0.04,
                            "total": 0.10,
                        }
                    },
                },
            },
            "api_usage_since_compaction": {
                "total_input_tokens": 3,
                "cached_input_tokens": 1,
                "total_output_tokens": 2,
                "approx_call_count": 1,
                "actual_call_count": 0,
                "estimated_call_count": 1,
                "api_confirmed_usage": {"actual_call_count": 0, "field_call_counts": {}},
            },
        }

    monkeypatch.setattr(diagnostics, "thread_token_stats", fake_thread_token_stats)

    registry.execute("schedulers", ctx)
    registry.execute("cost", ctx)

    assert any("No active" in message for message in logs)
    assert any(title == "Cost" for title, _text in printed)
    cost_text = next(text for title, text in printed if title == "Cost")
    assert "full_thread_context_tokens:       30" in cost_text
    assert "current_provider_context_tokens:  10" in cost_text
    assert "Full context usage (full effective history):" in cost_text
    assert "Current provider context usage (after last compaction):" in cost_text
    assert "total_input_tokens:    5" in cost_text
    assert "total_input_tokens:    3" in cost_text
    assert "cached_input_hit_rate: 20.0%" in cost_text
    assert "cache_creation_input_tokens: 2" in cost_text
    assert "actual_call_count:     1 API-confirmed" in cost_text
    assert "estimated_call_count:  0" in cost_text
    assert "API-confirmed usage:" in cost_text
    assert "input_tokens: 5" in cost_text
    assert "cached_input_tokens: 1" in cost_text
    assert "output_tokens: 2" in cost_text
    assert "cache_creation_input_tokens: 2" in cost_text
    assert "actual_call_count:     0 API-confirmed" in cost_text
    assert "estimated_call_count:  1" in cost_text
    assert "input_tokens: Not available" in cost_text
    assert "cached_input_tokens: Not available" in cost_text
    assert "output_tokens: Not available" in cost_text
    assert "cache_creation: $0.0300" in cost_text
    assert "calls=1 (actual=1, estimated=0)" in cost_text
    assert "cache_creation_in=2" in cost_text


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


def test_toggle_auto_continue_on_error_command(tmp_path) -> None:
    from eggthreads import ThreadsDB, create_child_thread, create_root_thread, get_thread_recovery

    registry = create_default_command_registry()
    logs: list[str] = []
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    root = create_root_thread(db, "recovery-toggle-root")
    child = create_child_thread(db, root, "recovery-toggle-child")

    ctx = CommandContext(db=db, current_thread=root, log_system=logs.append)

    assert get_thread_recovery(db, root).auto_continue_on_error is True
    registry.execute("toggleAutoContinueOnError", ctx)
    assert get_thread_recovery(db, root).auto_continue_on_error is False
    assert get_thread_recovery(db, child).auto_continue_on_error is False

    registry.execute("toggleAutoContinueOnError", ctx, "on")
    assert get_thread_recovery(db, root).auto_continue_on_error is True
    assert get_thread_recovery(db, child).auto_continue_on_error is True

    registry.execute("toggleAutoContinueOnError", ctx, "0")
    assert get_thread_recovery(db, root).auto_continue_on_error is False

    result = registry.execute("toggleAutoContinueOnError", ctx, "maybe")
    assert result.clear_input is False
    assert get_thread_recovery(db, root).auto_continue_on_error is False
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


def test_thread_ui_continue_appends_recovery_notice(tmp_path) -> None:
    from eggthreads import ThreadsDB, append_message, create_root_thread

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, "continue")
    user_msg_id = append_message(db, thread_id, "user", "Hello")
    append_message(db, thread_id, "assistant", "Partial answer")
    append_message(db, thread_id, "system", "LLM/runner error: provider exploded")

    logs: list[str] = []
    registry = create_default_command_registry()
    registry.execute(
        "continue",
        CommandContext(
            db=db,
            current_thread=thread_id,
            log_system=logs.append,
            print_current_thread=lambda **kwargs: None,
        ),
        user_msg_id,
    )

    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (thread_id,),
    ).fetchall()
    payloads = [json.loads(row[0]) for row in rows]
    notices = [payload for payload in payloads if payload.get("recovery_notice")]

    assert len(notices) == 1
    notice = notices[0]
    assert notice["role"] == "system"
    assert notice["no_api"] is True
    assert notice["preserve_on_continue"] is True
    assert "manual /continue" in notice["content"]
    assert "Previous error: LLM/runner error: provider exploded" in notice["content"]
    assert any("Continued from message" in message for message in logs)


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


def test_skills_commands_are_registered_handlers(tmp_path) -> None:
    from eggthreads.builtin_plugins import skills
    from eggthreads import ThreadsDB, create_root_thread, create_snapshot

    registry = create_default_command_registry()

    assert registry.get("skills").handler is skills.skills_command
    assert registry.get("skill").handler is skills.skill_command

    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = create_root_thread(db, "skills-test")
    create_snapshot(db, thread_id)
    logs: list[str] = []
    printed: list[tuple[str, str]] = []
    ctx = CommandContext(
        db=db,
        current_thread=thread_id,
        log_system=logs.append,
        console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
    )

    registry.execute("skills", ctx, "persistent REPL")
    registry.execute("skill", ctx, "rlm")
    registry.execute("skill", ctx, "")

    assert any(title == "Skills" and "SKILL SEARCH RESULTS" in text for title, text in printed)
    assert any(title.startswith("Skill: RLM Skill") and "chunk_text" in text for title, text in printed)
    assert any("Skill /rlm loaded" in message for message in logs)
    assert any("Usage: /skill <name>" in message for message in logs)
    assert "rlm" in registry.complete("skill", ctx, "r")


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


def test_display_input_commands_are_registered_handlers() -> None:
    from eggthreads.builtin_plugins import display_input

    registry = create_default_command_registry()

    assert registry.get("togglePanel").handler is display_input.toggle_panel_command
    assert registry.get("toggleBorders").handler is display_input.toggle_borders_command
    assert registry.get("redraw").handler is display_input.redraw_command
    assert registry.get("displayMode").handler is display_input.display_mode_command
    assert registry.get("displayVerbosity").handler is display_input.display_verbosity_command
    assert registry.get("paste").handler is display_input.paste_command
    assert registry.get("enterMode").handler is display_input.enter_mode_command


def test_compaction_commands_are_registered_handlers() -> None:
    from eggthreads.builtin_plugins import compaction

    registry = create_default_command_registry()

    assert registry.get("compact").handler is compaction.compact_thread_command
    assert registry.get("compactWithSummary").handler is compaction.compact_with_summary_command
    assert registry.get("context").handler is compaction.context_command
    assert registry.get("setAutoCompactThreshold").handler is compaction.set_auto_compact_threshold_command


def test_display_input_commands_change_app_state() -> None:
    registry = create_default_command_registry()

    logs: list[str] = []
    redrawn: list[str] = []

    class Style:
        box = object()

    class Panel:
        style = Style()

    class App:
        _panel_visible = {"chat": True, "children": True, "system": True}
        _display_is_inline = False
        _pending_mode_change = False
        _display_verbosity = "max"
        _borders_visible = False
        _original_box_styles = {"chat": "chat-box", "system": "system-box", "children": "children-box", "approval": "approval-box"}
        chat_output = Panel()
        system_output = Panel()
        children_output = Panel()
        approval_panel = Panel()
        enter_sends = True

        def redraw_static_view(self, reason=None):
            redrawn.append(reason)

    app = App()
    ctx = CommandContext(app=app, log_system=logs.append)

    registry.execute("togglePanel", ctx, "chat")
    registry.execute("displayMode", ctx, "inline")
    registry.execute("displayVerbosity", ctx, "min")
    registry.execute("redraw", ctx)
    registry.execute("enterMode", ctx, "newline")

    assert app._panel_visible["chat"] is False
    assert app._display_is_inline is True
    assert app._pending_mode_change is True
    assert app._display_verbosity == "min"
    assert app.enter_sends is False
    assert "manual" in redrawn


def test_web_commands_are_registered_handlers(monkeypatch) -> None:
    from eggthreads.builtin_plugins import web

    registry = create_default_command_registry()

    assert registry.get("startSearxng").handler is web.start_searxng_command
    assert registry.get("stopSearxng").handler is web.stop_searxng_command

    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(web, "run_searxng_compose", lambda ctx, args, **kwargs: calls.append((args, kwargs["action"])))

    registry.execute("startSearxng", CommandContext())
    registry.execute("stopSearxng", CommandContext())

    assert calls == [(["up", "-d"], "start"), (["down"], "stop")]


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
