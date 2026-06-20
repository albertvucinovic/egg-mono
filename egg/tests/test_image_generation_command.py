from __future__ import annotations

import json
from pathlib import Path


SHA = "0123456789abcdef" * 4


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



def _snapshot_messages(app):
    from eggthreads import create_snapshot

    create_snapshot(app.db, app.current_thread)
    snap = json.loads(app.db.get_thread(app.current_thread).snapshot_json)
    return snap["messages"]



def test_image_generate_command_invokes_service_appends_artifact_refs_and_renders(egg_app, monkeypatch, tmp_path):
    import egg.image_generation as image_generation

    calls = []

    def fake_generate(workspace, thread_id, prompt, *, model_key, models_path, all_models_path, options):
        calls.append(
            {
                "workspace": Path(workspace),
                "thread_id": thread_id,
                "prompt": prompt,
                "model_key": model_key,
                "models_path": Path(models_path),
                "all_models_path": Path(all_models_path),
                "options": options,
            }
        )
        return _FakeImageGenerationResult(thread_id=thread_id, prompt=prompt)

    monkeypatch.setattr(image_generation, "generate_openai_image_artifacts", fake_generate)

    assert "imageGenerate" in egg_app.command_registry.names()

    egg_app.handle_command(
        '/imageGenerate model="Image Backend" n=2 size=1024x1024 '
        'quality=high output_format=webp Paint a bright egg'
    )

    assert calls == [
        {
            "workspace": tmp_path.resolve(),
            "thread_id": egg_app.current_thread,
            "prompt": "Paint a bright egg",
            "model_key": "Image Backend",
            "models_path": calls[0]["models_path"],
            "all_models_path": calls[0]["all_models_path"],
            "options": {
                "n": 2,
                "size": "1024x1024",
                "quality": "high",
                "output_format": "webp",
            },
        }
    ]

    messages = _snapshot_messages(egg_app)
    generated = [
        m
        for m in messages
        if isinstance(m.get("content"), list)
        and any(part.get("type") == "artifact" for part in m["content"] if isinstance(part, dict))
    ]
    assert len(generated) == 1
    message = generated[0]
    assert message["role"] == "assistant"
    assert not message.get("no_api")
    content = message["content"]
    assert content[0]["type"] == "text"
    assert "Generated 1 image artifact" in content[0]["text"]
    assert "Paint a bright egg" in content[0]["text"]
    assert content[1]["type"] == "artifact"
    assert content[1]["artifact_id"] == "abc12345"
    assert content[1]["provenance"] == {
        "kind": "openai_image_generation",
        "provider": "openai-images",
        "model_key": "Image Backend",
    }
    assert not any("generated-bytes" in json.dumps(part) for part in content)
    assert any("Provider artifact: image generated-1.png" in entry for entry in egg_app._system_log)



def test_image_generate_command_requires_prompt_and_does_not_call_service(egg_app, monkeypatch):
    import egg.image_generation as image_generation

    called = False

    def fake_generate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("service should not be called without a prompt")

    monkeypatch.setattr(image_generation, "generate_openai_image_artifacts", fake_generate)

    egg_app.handle_command("/imageGenerate n=1")

    assert called is False
    assert any("Usage: /imageGenerate" in entry for entry in egg_app._system_log)
    assert not [
        m
        for m in _snapshot_messages(egg_app)
        if isinstance(m.get("content"), list)
        and any(part.get("type") == "artifact" for part in m["content"] if isinstance(part, dict))
    ]



def test_image_generate_command_reports_service_failure_without_appending_result(egg_app, monkeypatch):
    import egg.image_generation as image_generation

    def fake_generate(*args, **kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(image_generation, "generate_openai_image_artifacts", fake_generate)

    egg_app.handle_command("/imageGenerate a small egg")

    assert any("/imageGenerate failed: backend unavailable" in entry for entry in egg_app._system_log)
    assert not [
        m
        for m in _snapshot_messages(egg_app)
        if isinstance(m.get("content"), list)
        and any(part.get("type") == "artifact" for part in m["content"] if isinstance(part, dict))
    ]
