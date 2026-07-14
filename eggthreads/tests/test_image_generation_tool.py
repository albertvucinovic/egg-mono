from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


SHA = "0123456789abcdef" * 4


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


class _FakeGeneratedArtifact:
    artifact_id = "abc12345"

    def __init__(self, thread_id: str):
        self.content_part = {
            "type": "artifact",
            "artifact_id": self.artifact_id,
            "owner_thread_id": thread_id,
            "presentation": "image",
            "mime_type": "image/png",
            "filename": "generated-1.png",
            "size_bytes": 11,
            "sha256": SHA,
            "provenance": {
                "kind": "openai_image_generation",
                "provider": "openai-images",
                "model_key": "Image Backend",
            },
            "options": {},
        }
        self.metadata = {
            "artifact_id": self.artifact_id,
            "owner_thread_id": thread_id,
            "presentation": "image",
            "mime_type": "image/png",
            "filename": "generated-1.png",
            "size_bytes": 11,
            "sha256": SHA,
            "blob_relpath": "../../_blobs/sha256/01/" + SHA,
            "provider_refs": {"openai": {"source": "b64_json"}},
        }


class _FakeImageGenerationResult:
    def __init__(self, *, thread_id: str, prompt: str):
        self.model_key = "Image Backend"
        self.provider_name = "openai-images"
        self.model_name = "gpt-image-1"
        self.prompt = prompt
        self.response_metadata = {"id": "img-resp-test"}
        self.artifacts = (_FakeGeneratedArtifact(thread_id),)

    @property
    def content_parts(self):
        return [artifact.content_part for artifact in self.artifacts]

    @property
    def metadata(self):
        return [artifact.metadata for artifact in self.artifacts]


def test_generate_image_tool_registered_and_exposed_with_intended_schema():
    tools = ts.create_default_tools()
    specs = {spec["function"]["name"]: spec["function"] for spec in tools.tools_spec()}

    assert "generate_image" in specs
    spec = specs["generate_image"]
    assert "provider-output artifacts" in spec["description"]
    parameters = spec["parameters"]
    assert parameters["required"] == ["prompt"]
    assert parameters["additionalProperties"] is False
    props = parameters["properties"]
    for name in ("prompt", "model", "backend", "n", "size", "quality", "output_format", "background"):
        assert name in props
    assert props["prompt"]["type"] == "string"
    assert props["n"]["type"] == "integer"
    assert props["timeout"]["type"] == "number"
    assert "models_path" not in props
    assert "all_models_path" not in props
    assert "bytes" not in props
    assert "base64" not in props


def test_generate_image_tool_calls_shared_service_and_returns_artifact_metadata(tmp_path, monkeypatch):
    import eggthreads.builtin_plugins.image_generation as image_generation_tool

    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    models_path = tmp_path / "models.json"
    all_models_path = tmp_path / "all-models.json"
    image_generation_models_path = tmp_path / "image-generation-models.json"
    calls = []
    before_message_count = db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE thread_id=? AND type='msg.create'",
        (thread_id,),
    ).fetchone()[0]

    def fake_generate(
        workspace_arg,
        thread_id_arg,
        prompt,
        *,
        model_key,
        models_path,
        all_models_path,
        image_generation_models_path,
        options,
        timeout,
    ):
        calls.append(
            {
                "workspace": Path(workspace_arg),
                "thread_id": thread_id_arg,
                "prompt": prompt,
                "model_key": model_key,
                "models_path": Path(models_path),
                "all_models_path": Path(all_models_path),
                "image_generation_models_path": Path(image_generation_models_path),
                "options": options,
                "timeout": timeout,
            }
        )
        return _FakeImageGenerationResult(thread_id=thread_id_arg, prompt=prompt)

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    output = ts.create_default_tools().execute(
        "generate_image",
        {
            "prompt": "Paint a bright egg",
            "backend": "Image Backend",
            "n": 2,
            "size": "1024x1024",
            "quality": "high",
            "output_format": "jpg",
            "background": "transparent",
            "timeout": 17,
        },
        thread_id=thread_id,
        db=db,
        working_dir=workspace,
        models_path=models_path,
        all_models_path=all_models_path,
        image_generation_models_path=image_generation_models_path,
    )

    assert calls == [
        {
            "workspace": workspace.resolve(),
            "thread_id": thread_id,
            "prompt": "Paint a bright egg",
            "model_key": "Image Backend",
            "models_path": models_path,
            "all_models_path": all_models_path,
            "image_generation_models_path": image_generation_models_path,
            "options": {
                "n": 2,
                "size": "1024x1024",
                "quality": "high",
                "output_format": "jpeg",
                "background": "transparent",
            },
            "timeout": 17,
        }
    ]
    payload = json.loads(output)
    assert payload["prompt"] == "Paint a bright egg"
    assert payload["model_key"] == "Image Backend"
    assert payload["provider_name"] == "openai-images"
    assert payload["model_name"] == "gpt-image-1"
    assert payload["artifact_count"] == 1
    assert payload["artifacts"] == [
        {
            "artifact_id": "abc12345",
            "owner_thread_id": thread_id,
            "presentation": "image",
            "mime_type": "image/png",
            "filename": "generated-1.png",
            "size_bytes": 11,
            "sha256": SHA,
        }
    ]
    assert payload["content_parts"][0]["type"] == "text"
    assert payload["content_parts"][1]["artifact_id"] == "abc12345"
    assert "Provider artifact: image generated-1.png" in payload["content_text"]
    encoded = json.dumps(payload)
    assert "generated-bytes" not in encoded
    assert "b64_json" not in encoded
    assert "base64" not in encoded
    assert "blob_relpath" not in encoded
    after_message_count = db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE thread_id=? AND type='msg.create'",
        (thread_id,),
    ).fetchone()[0]
    assert after_message_count == before_message_count


def test_generate_image_tool_publishes_artifact_content_parts_in_runner_transcript(tmp_path, monkeypatch):
    import asyncio
    import eggthreads.builtin_plugins.image_generation as image_generation_tool
    from eggthreads.tools import ToolRegistry

    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")
    tool_call_id = "call-generate-image"
    ts.append_message(
        db,
        thread_id,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "generate_image", "arguments": json.dumps({"prompt": "Paint an egg"})},
                }
            ]
        },
    )
    db.append_event("approve", thread_id, "tool_call.approval", {"tool_call_id": tool_call_id, "decision": "granted"})

    def fake_generate(*args, **kwargs):
        return _FakeImageGenerationResult(thread_id=thread_id, prompt="Paint an egg")

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    tools = ToolRegistry()
    image_generation_tool.register_image_generation_tools(tools)
    runner = ts.ThreadRunner(db, thread_id, llm=object(), tools=tools)

    assert asyncio.run(runner.run_once()) is True
    assert ts.build_tool_call_states(db, thread_id)[tool_call_id].state == "TC5"
    assert asyncio.run(runner.run_once()) is True

    messages = ts.create_snapshot(db, thread_id)["messages"]
    tool_message = next(msg for msg in messages if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id)
    assert tool_message["name"] == "generate_image"
    content = tool_message["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "Generated 1 image artifact" in content[0]["text"]
    assert content[1]["type"] == "artifact"
    assert content[1]["artifact_id"] == "abc12345"
    assert "Provider artifact: image generated-1.png" in ts.content_to_plain_text(content)


def test_generate_image_tool_rejects_missing_prompt_without_calling_service(monkeypatch):
    import eggthreads.builtin_plugins.image_generation as image_generation_tool

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service should not be called without a prompt")

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    output = ts.create_default_tools().execute("generate_image", {"prompt": "   "}, thread_id="thread-1", db=object())

    assert output == "Error: image generation prompt is required."
    assert called is False


def test_generate_image_tool_requires_thread_and_database_context(monkeypatch):
    import eggthreads.builtin_plugins.image_generation as image_generation_tool

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service should not be called without context")

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    output = ts.create_default_tools().execute("generate_image", {"prompt": "Paint an egg"})

    assert output == "Error: generate_image requires a current thread and database context."
    assert called is False


def test_generate_image_tool_rejects_invalid_output_format_without_calling_service(monkeypatch):
    import eggthreads.builtin_plugins.image_generation as image_generation_tool

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service should not be called for invalid output_format")

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    output = ts.create_default_tools().execute(
        "generate_image",
        {"prompt": "Paint an egg", "output_format": "gif"},
        thread_id="thread-1",
        db=object(),
    )

    assert output == "Error: output_format must be png, jpeg, or webp"
    assert called is False


def test_generate_image_tool_rejects_invalid_n_without_calling_service(monkeypatch):
    import eggthreads.builtin_plugins.image_generation as image_generation_tool

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service should not be called for invalid n")

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    output = ts.create_default_tools().execute(
        "generate_image",
        {"prompt": "Paint an egg", "n": 0},
        thread_id="thread-1",
        db=object(),
    )

    assert output == "Error: n must be an integer from 1 to 10"
    assert called is False


def test_generate_image_tool_rejects_conflicting_model_backend_without_calling_service(monkeypatch):
    import eggthreads.builtin_plugins.image_generation as image_generation_tool

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service should not be called for conflicting model/backend")

    monkeypatch.setattr(image_generation_tool, "generate_openai_image_artifacts", fake_generate)

    output = ts.create_default_tools().execute(
        "generate_image",
        {"prompt": "Paint an egg", "model": "Image A", "backend": "Image B"},
        thread_id="thread-1",
        db=object(),
    )

    assert output == "Error: model and backend must match when both are provided"
    assert called is False
