from pathlib import Path

from egg.attachments import staged_attachments_for_thread


def test_terminal_quick_start_prefills_unsent_draft(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_FORCE_WITHOUT_AIOHTTP", "1")
    monkeypatch.delenv("EGG_RELOAD_THREAD_ID", raising=False)
    from egg.app import EggDisplayApp

    monkeypatch.setattr(EggDisplayApp, "start_scheduler", lambda self, root_tid: None)
    app = EggDisplayApp(quick_start_args=["Tell", "me a story"])

    assert app.input_panel.get_text() == "Tell me a story"
    assert staged_attachments_for_thread(app, app.current_thread) == []


def test_terminal_runtime_flag_is_not_inserted_into_draft(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_FORCE_WITHOUT_AIOHTTP", "1")
    monkeypatch.delenv("EGG_RELOAD_THREAD_ID", raising=False)
    from egg.app import EggDisplayApp

    monkeypatch.setattr(EggDisplayApp, "start_scheduler", lambda self, root_tid: None)
    app = EggDisplayApp(quick_start_args=["--force-without-aiohttp", "Tell", "me"])

    assert app.input_panel.get_text() == "Tell me"


def test_terminal_quick_start_stages_sole_file_without_inlining(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_FORCE_WITHOUT_AIOHTTP", "1")
    monkeypatch.delenv("EGG_RELOAD_THREAD_ID", raising=False)
    source = tmp_path / "notes with spaces.txt"
    source.write_text("attachment bytes only", encoding="utf-8")
    from egg.app import EggDisplayApp

    monkeypatch.setattr(EggDisplayApp, "start_scheduler", lambda self, root_tid: None)
    app = EggDisplayApp(quick_start_args=[source.name])

    assert app.input_panel.get_text() == ""
    staged = staged_attachments_for_thread(app, app.current_thread)
    assert len(staged) == 1
    assert staged[0]["filename"] == source.name
    assert staged[0]["type"] == "attachment"


def test_terminal_reload_does_not_reapply_quick_start(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_FORCE_WITHOUT_AIOHTTP", "1")
    monkeypatch.setenv("EGG_RELOAD_THREAD_ID", "reload-attempt")
    from egg.app import EggDisplayApp

    monkeypatch.setattr(EggDisplayApp, "start_scheduler", lambda self, root_tid: None)
    app = EggDisplayApp(quick_start_args=["must", "not overwrite"])

    assert app.input_panel.get_text() == ""
    assert staged_attachments_for_thread(app, app.current_thread) == []
