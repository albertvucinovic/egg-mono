from __future__ import annotations

import json

import eggthreads as ts
from eggthreads.command_catalog import CommandContext, create_default_command_registry
from eggthreads.tools import ToolExecutionResult, create_default_tools


def _make_db(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _set_recap(db, thread_id: str, recap: str) -> None:
    db.conn.execute(
        "UPDATE threads SET short_recap=? WHERE thread_id=?",
        (recap, thread_id),
    )


def test_threads_tool_returns_nested_calling_subtree(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="worker")
    grandchild = ts.create_child_thread(db, child, name="reviewer")
    second_child = ts.create_child_thread(db, root, name="second")
    unrelated = ts.create_root_thread(db, name="unrelated")
    _set_recap(db, root, "root description")
    _set_recap(db, child, "worker description")
    _set_recap(db, grandchild, "review description")
    _set_recap(db, second_child, "second description")

    output = create_default_tools().execute("threads", {}, db=db, thread_id=root)

    assert json.loads(output) == {
        "root_thread_id": root,
        "status_mode": "fast",
        "runnability_checked": False,
        "total": 4,
        "root_count": 1,
        "thread_ids": [root, child, grandchild, second_child],
        "threads": [
            {
                "id": root,
                "name": "root",
                "description": "root description",
                "state": "idle",
                "model": None,
                "children": [
                    {
                        "id": child,
                        "name": "worker",
                        "description": "worker description",
                        "state": "idle",
                        "model": None,
                        "children": [
                            {
                                "id": grandchild,
                                "name": "reviewer",
                                "description": "review description",
                                "state": "idle",
                                "model": None,
                                "children": [],
                            }
                        ],
                    },
                    {
                        "id": second_child,
                        "name": "second",
                        "description": "second description",
                        "state": "idle",
                        "model": None,
                        "children": [],
                    },
                ],
            }
        ],
    }
    assert unrelated not in output


def test_threads_tool_can_narrow_to_descendant_subtree(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    output = create_default_tools().execute(
        "threads", {"thread_id": child}, db=db, thread_id=root
    )
    payload = json.loads(output)

    assert payload["root_thread_id"] == child
    assert payload["threads"][0]["id"] == child
    assert payload["threads"][0]["children"][0]["id"] == grandchild
    assert root not in output


def test_threads_tool_reports_streaming_state_and_effective_model(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    ts.set_thread_model(db, child, "child-model", reason="test")
    assert db.try_open_stream(
        child,
        "streaming-invoke",
        "2999-01-01 00:00:00",
        owner="test",
        purpose="assistant_stream",
    )

    payload = json.loads(
        create_default_tools().execute("threads", {}, db=db, thread_id=root)
    )
    child_node = payload["threads"][0]["children"][0]

    assert child_node["state"] == "streaming"
    assert child_node["model"] == "child-model"


def test_threads_tool_full_status_reports_runnable_while_fast_stays_bounded(
    tmp_path, monkeypatch
) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    calls = []

    import eggthreads.api as thread_api

    def fake_statuses(_db, thread_ids, *, skip_runnability=False):
        calls.append(skip_runnability)
        return {
            thread_id: (
                "runnable" if thread_id == child and not skip_runnability else "idle"
            )
            for thread_id in thread_ids
        }

    monkeypatch.setattr(thread_api, "get_thread_statuses_bulk", fake_statuses)
    tools = create_default_tools()

    fast = json.loads(tools.execute("threads", {}, db=db, thread_id=root))
    full = json.loads(
        tools.execute("threads", {"status": "full"}, db=db, thread_id=root)
    )

    assert calls == [True, False]
    assert fast["status_mode"] == "fast"
    assert fast["runnability_checked"] is False
    assert full["status_mode"] == "full"
    assert full["runnability_checked"] is True
    assert fast["threads"][0]["children"][0]["state"] == "idle"
    assert full["threads"][0]["children"][0]["state"] == "runnable"


def test_threads_tool_denies_ancestors_siblings_unrelated_and_missing(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    sibling = ts.create_child_thread(db, root, name="sibling")
    unrelated = ts.create_root_thread(db, name="unrelated")
    tools = create_default_tools()

    for target in (root, sibling, unrelated, "missing-thread"):
        result = tools.execute(
            "threads",
            {"thread_id": target},
            db=db,
            thread_id=child,
            preserve_tool_result=True,
        )
        assert isinstance(result, ToolExecutionResult)
        assert result.reason == "denied"
        assert "calling thread or one of its descendants" in result.output


def test_threads_tool_called_from_descendant_never_lists_sibling_or_ancestor(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")
    sibling = ts.create_child_thread(db, root, name="sibling")

    output = create_default_tools().execute("threads", {}, db=db, thread_id=child)
    payload = json.loads(output)

    assert payload["root_thread_id"] == child
    assert payload["threads"][0]["id"] == child
    assert payload["threads"][0]["children"][0]["id"] == grandchild
    assert root not in output
    assert sibling not in output


def test_selected_subtree_query_does_not_load_unrelated_thread_metadata(
    tmp_path, monkeypatch
) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")
    unrelated = ts.create_root_thread(db, name="unrelated")
    loaded = []
    original = db.get_thread_metadata

    def record(thread_id):
        loaded.append(thread_id)
        return original(thread_id)

    monkeypatch.setattr(db, "get_thread_metadata", record)

    tree = ts.get_thread_tree(db, child)

    assert tree[0]["id"] == child
    assert tree[0]["children"][0]["id"] == grandchild
    assert set(loaded) == {child}
    assert root not in loaded
    assert unrelated not in loaded


def test_threads_tool_fails_closed_when_descendant_has_multiple_parents(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    sibling = ts.create_child_thread(db, root, name="sibling")
    db.conn.execute(
        "INSERT INTO children(parent_id, child_id) VALUES (?, ?)",
        (child, sibling),
    )

    result = create_default_tools().execute(
        "threads", {}, db=db, thread_id=child, preserve_tool_result=True
    )

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "error"
    assert "multiple parents" in result.output
    assert sibling not in result.output


def test_thread_tree_handles_depth_beyond_python_recursion_limit(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    parent = root
    for index in range(1100):
        child = f"deep-{index:04d}"
        db.create_thread(child, name=child, parent_id=parent, depth=index + 1)
        parent = child

    tree = ts.get_thread_tree(db, root)
    node = tree[0]
    depth = 0
    while node["children"]:
        node = node["children"][0]
        depth += 1

    assert depth == 1100

    output = create_default_tools().execute("threads", {}, db=db, thread_id=root)
    assert output.startswith(f'{{"root_thread_id":"{root}","status_mode":"fast"')
    assert '"id":"deep-1099"' in output


def test_threads_tool_requires_calling_context(tmp_path) -> None:
    db = _make_db(tmp_path)
    ts.create_root_thread(db, name="root")

    result = create_default_tools().execute(
        "threads", {}, db=db, preserve_tool_result=True
    )

    assert isinstance(result, ToolExecutionResult)
    assert result.reason == "error"
    assert "requires a calling thread" in result.output


def test_threads_tool_is_registered_with_expected_schema() -> None:
    tools = create_default_tools()
    specs = {spec["function"]["name"]: spec["function"] for spec in tools.tools_spec()}

    assert "threads" in specs
    parameters = specs["threads"]["parameters"]
    assert set(parameters["properties"]) == {"thread_id", "status", "timeout"}
    assert parameters["additionalProperties"] is False
    assert parameters.get("required", []) == []


def test_threads_command_selector_uses_shared_tree_data(tmp_path, monkeypatch) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")
    captured = []

    from eggthreads.builtin_plugins import thread_ui

    original = thread_ui._thread_tree

    def capture_tree(db_arg, root_thread_id=None, **kwargs):
        captured.append(root_thread_id)
        return original(db_arg, root_thread_id, **kwargs)

    monkeypatch.setattr(thread_ui, "_thread_tree", capture_tree)
    logs = []
    context = CommandContext(
        db=db,
        current_thread=root,
        select_threads=lambda selector: [child] if selector == child else [],
        log_system=logs.append,
    )

    result = create_default_command_registry().execute("threads", context, child)

    assert result.clear_input is True
    assert captured == [child]
    rendered = json.loads(logs[-1])
    assert rendered["threads"][0]["id"] == child
    assert grandchild in json.dumps(original(db, child))


def test_threads_command_rejects_ambiguous_selector(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    ts.create_child_thread(db, root, name="duplicate worker")
    ts.create_child_thread(db, root, name="duplicate worker")
    logs = []

    result = create_default_command_registry().execute(
        "threads",
        CommandContext(db=db, current_thread=root, log_system=logs.append),
        "duplicate",
    )

    assert result.clear_input is False
    assert logs == ["ambiguous thread selector: duplicate"]


def test_parse_thread_tree_request_preserves_selector_and_status_modes() -> None:
    assert ts.parse_thread_tree_request("") == (None, "fast")
    assert ts.parse_thread_tree_request("full") == (None, "full")
    assert ts.parse_thread_tree_request("status=full") == (None, "full")
    assert ts.parse_thread_tree_request("thread-id") == ("thread-id", "fast")
    assert ts.parse_thread_tree_request("thread-id status=full") == (
        "thread-id",
        "full",
    )


def test_threads_command_forwards_full_status_to_terminal_formatter(tmp_path) -> None:
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    calls = []
    printed = []

    def format_threads(selected=None, *, include_runnability=False):
        calls.append((selected, include_runnability))
        return "rendered"

    result = create_default_command_registry().execute(
        "threads",
        CommandContext(
            db=db,
            current_thread=root,
            format_threads=format_threads,
            console_print_block=lambda title, text, **kwargs: printed.append(
                (title, text)
            ),
        ),
        "status=full",
    )

    assert result.clear_input is True
    assert calls == [(None, True)]
    assert printed == [("Threads", "rendered")]
