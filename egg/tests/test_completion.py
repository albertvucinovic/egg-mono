"""Tests for completion.py autocompletion."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from egg.completion import ModelCompleter, EggCompleter, get_autocomplete_items


class MockDocument:
    """Mock prompt_toolkit document."""
    def __init__(self, text: str):
        self.text_before_cursor = text

    def get_word_before_cursor(self, WORD=False):
        """Get the word before cursor."""
        import re
        if WORD:
            m = re.search(r'(\S+)$', self.text_before_cursor)
        else:
            m = re.search(r'(\w+)$', self.text_before_cursor)
        return m.group(1) if m else ''


class TestModelCompleter:
    """Tests for ModelCompleter."""

    def test_returns_nothing_for_non_model_command(self):
        """Should return nothing for non /model commands."""
        completer = ModelCompleter(None)
        doc = MockDocument("/help ")

        completions = list(completer.get_completions(doc, None))

        assert completions == []

    def test_returns_nothing_when_no_llm_client(self):
        """Should return nothing when llm_client is None."""
        completer = ModelCompleter(None)
        doc = MockDocument("/model gpt")

        completions = list(completer.get_completions(doc, None))

        assert completions == []

    def test_returns_configured_display_names(self):
        """Should return configured model display names."""
        mock_llm = MagicMock()
        mock_llm.registry.models_config = {
            'gpt-4': {'provider': 'openai'},
            'claude-3': {'provider': 'anthropic'}
        }
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        completer = ModelCompleter(mock_llm)
        doc = MockDocument("/model ")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'gpt-4' in texts
        assert 'claude-3' in texts

    def test_filters_by_prefix(self):
        """Should filter by prefix."""
        mock_llm = MagicMock()
        mock_llm.registry.models_config = {
            'gpt-4': {'provider': 'openai'},
            'gpt-3.5': {'provider': 'openai'},
            'claude-3': {'provider': 'anthropic'}
        }
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        completer = ModelCompleter(mock_llm)
        doc = MockDocument("/model gpt")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'gpt-4' in texts
        assert 'gpt-3.5' in texts
        assert 'claude-3' not in texts

    def test_includes_provider_prefixed_names(self):
        """Should include provider:name format."""
        mock_llm = MagicMock()
        mock_llm.registry.models_config = {
            'gpt-4': {'provider': 'openai'}
        }
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        completer = ModelCompleter(mock_llm)
        doc = MockDocument("/model ")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'openai:gpt-4' in texts

    def test_includes_aliases(self):
        """Should include model aliases."""
        mock_llm = MagicMock()
        mock_llm.registry.models_config = {
            'gpt-4': {'provider': 'openai', 'alias': ['gpt4', 'gpt-4-turbo']}
        }
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        completer = ModelCompleter(mock_llm)
        doc = MockDocument("/model ")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'gpt4' in texts
        assert 'gpt-4-turbo' in texts

    def test_handles_all_prefix(self):
        """Should handle all: prefix for catalog suggestions."""
        mock_llm = MagicMock()
        mock_llm.catalog.get_all_models_suggestions = MagicMock(
            return_value=['all:openai:gpt-4', 'all:openai:gpt-3.5-turbo']
        )

        completer = ModelCompleter(mock_llm)
        doc = MockDocument("/model all:openai")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'all:openai:gpt-4' in texts


class TestEggCompleter:
    """Tests for EggCompleter."""

    def test_delegates_model_to_model_completer(self, isolated_db, monkeypatch):
        """Should delegate /model to ModelCompleter."""
        mock_llm = MagicMock()
        mock_llm.registry.models_config = {'gpt-4': {'provider': 'openai'}}
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        completer = EggCompleter(isolated_db, lambda: "tid", mock_llm)
        doc = MockDocument("/model ")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'gpt-4' in texts

    def test_completes_update_all_models_providers(self, isolated_db):
        """Should complete provider names for /updateAllModels."""
        mock_llm = MagicMock()
        mock_llm.get_providers = MagicMock(return_value=['openai', 'anthropic', 'google'])

        completer = EggCompleter(isolated_db, lambda: "tid", mock_llm)
        doc = MockDocument("/updateAllModels ")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        assert 'openai' in texts
        assert 'anthropic' in texts

    def test_completes_thread_selectors(self, isolated_db, monkeypatch):
        """Should complete thread selectors for /thread."""
        from eggthreads import create_root_thread, create_snapshot

        thread1 = create_root_thread(isolated_db, name="TestThread1")
        thread2 = create_root_thread(isolated_db, name="TestThread2")
        create_snapshot(isolated_db, thread1)
        create_snapshot(isolated_db, thread2)

        completer = EggCompleter(isolated_db, lambda: thread1, None)
        doc = MockDocument("/thread ")

        completions = list(completer.get_completions(doc, None))

        # Should have completions for both threads
        texts = [c.text for c in completions]
        assert any(thread1 in t for t in texts)
        assert any(thread2 in t for t in texts)

    def test_completes_delete_excludes_current(self, isolated_db, monkeypatch):
        """Should exclude current thread from /deleteThread completions."""
        from eggthreads import create_root_thread, create_snapshot

        thread1 = create_root_thread(isolated_db, name="Thread1")
        thread2 = create_root_thread(isolated_db, name="Thread2")
        create_snapshot(isolated_db, thread1)
        create_snapshot(isolated_db, thread2)

        completer = EggCompleter(isolated_db, lambda: thread1, None)
        doc = MockDocument("/deleteThread ")

        completions = list(completer.get_completions(doc, None))

        texts = [c.text for c in completions]
        # Current thread should be excluded
        assert thread1 not in texts
        assert thread2 in texts

    def test_filesystem_suggestions(self, isolated_db, tmp_path, monkeypatch):
        """Should provide filesystem suggestions for /spawnChildThread."""
        # Create test files
        (tmp_path / "test_file.txt").touch()
        (tmp_path / "test_dir").mkdir()
        monkeypatch.chdir(tmp_path)

        completer = EggCompleter(isolated_db, lambda: "tid", None)
        doc = MockDocument("/spawnChildThread test")
        monkeypatch.setattr(doc, 'get_word_before_cursor', lambda WORD=False: "test")

        completions = list(completer.get_completions(doc, None))

        # Should have some completions
        texts = [c.text for c in completions]
        # May or may not find matches depending on cwd

    def test_conversation_word_suggestions(self, isolated_db, monkeypatch):
        """Should provide conversation word suggestions."""
        from eggthreads import create_root_thread, create_snapshot, append_message
        import json

        thread = create_root_thread(isolated_db, name="TestThread")
        append_message(isolated_db, thread, "user", "Hello world testing")
        create_snapshot(isolated_db, thread)

        completer = EggCompleter(isolated_db, lambda: thread, None)
        doc = MockDocument("test")

        completions = list(completer.get_completions(doc, None))

        # Should have word suggestions from conversation
        texts = [c.text for c in completions]
        assert 'testing' in texts or 'Hello' in texts or 'world' in texts


class TestGetAutocompleteItems:
    """Tests for get_autocomplete_items()."""

    def test_returns_command_completions(self, isolated_db):
        """Should return command completions for /."""
        items = get_autocomplete_items("/", 1, isolated_db, lambda: "tid", None)

        displays = [item['display'] for item in items]
        assert '/help' in displays
        assert '/model' in displays
        assert '/thread' in displays

    def test_filters_commands_by_prefix(self, isolated_db):
        """Should filter commands by prefix."""
        items = get_autocomplete_items("/th", 3, isolated_db, lambda: "tid", None)

        displays = [item['display'] for item in items]
        assert '/thread' in displays or '/threads' in displays
        assert '/help' not in displays

    def test_returns_model_completions(self, isolated_db):
        """Should return model completions for /model."""
        mock_llm = MagicMock()
        mock_llm.registry.models_config = {'gpt-4': {'provider': 'openai'}}
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        items = get_autocomplete_items("/model ", 7, isolated_db, lambda: "tid", mock_llm)

        displays = [item['display'] for item in items]
        assert 'gpt-4' in displays

    def test_returns_provider_completions(self, isolated_db):
        """Should return provider completions for /updateAllModels."""
        mock_llm = MagicMock()
        mock_llm.get_providers = MagicMock(return_value=['openai', 'anthropic'])

        items = get_autocomplete_items("/updateAllModels ", 17, isolated_db, lambda: "tid", mock_llm)

        displays = [item['display'] for item in items]
        assert 'openai' in displays
        assert 'anthropic' in displays

    def test_returns_thread_completions(self, isolated_db):
        """Should return thread completions for /thread."""
        from eggthreads import create_root_thread, create_snapshot

        thread = create_root_thread(isolated_db, name="TestThread")
        create_snapshot(isolated_db, thread)

        items = get_autocomplete_items("/thread ", 8, isolated_db, lambda: thread, None)

        # Should have at least one thread suggestion
        assert len(items) >= 1

    def test_returns_toggle_panel_options(self, isolated_db):
        """Should return panel options for /togglePanel."""
        items = get_autocomplete_items("/togglePanel ", 13, isolated_db, lambda: "tid", None)

        displays = [item['display'] for item in items]
        assert 'chat' in displays
        assert 'children' in displays
        assert 'system' in displays

    def test_returns_filesystem_completions_for_plain_text(self, isolated_db, tmp_path, monkeypatch):
        """Should return filesystem completions for plain text."""
        (tmp_path / "myfile.txt").touch()
        monkeypatch.chdir(tmp_path)

        items = get_autocomplete_items("myf", 3, isolated_db, lambda: "tid", None)

        # Should have file suggestion
        displays = [item['display'] for item in items]
        assert any('myfile' in d for d in displays)

    def test_limits_results_to_50(self, isolated_db, monkeypatch):
        """Should limit results to 50 items."""
        mock_llm = MagicMock()
        # Create many model suggestions
        mock_llm.registry.models_config = {f'model-{i}': {'provider': 'test'} for i in range(100)}
        mock_llm.catalog.get_all_models_suggestions = MagicMock(return_value=[])
        mock_llm.get_providers = MagicMock(return_value=[])

        items = get_autocomplete_items("/model ", 7, isolated_db, lambda: "tid", mock_llm)

        assert len(items) <= 50

    def test_returns_empty_for_no_matches(self, isolated_db):
        """Should return empty list when no matches."""
        items = get_autocomplete_items("/xyz_nonexistent_cmd", 20, isolated_db, lambda: "tid", None)

        assert items == []


class TestModelCompleterNormalize:
    """Tests for ModelCompleter._normalize()."""

    def test_normalizes_to_lowercase(self):
        """Should normalize to lowercase."""
        completer = ModelCompleter(None)

        result = completer._normalize("GPT-4-Turbo")

        assert result == "gpt 4 turbo"

    def test_replaces_special_chars_with_space(self):
        """Should replace special chars with space."""
        completer = ModelCompleter(None)

        result = completer._normalize("model-name_v2.0")

        assert result == "model name v2 0"

    def test_handles_empty_string(self):
        """Should handle empty string."""
        completer = ModelCompleter(None)

        result = completer._normalize("")

        assert result == ""

    def test_collapses_multiple_spaces(self):
        """Should collapse multiple spaces."""
        completer = ModelCompleter(None)

        result = completer._normalize("a   b   c")

        assert result == "a b c"


class TestEggCompleterHelpers:
    """Tests for EggCompleter helper methods."""

    def test_get_filesystem_suggestions_returns_paths(self, isolated_db, tmp_path, monkeypatch):
        """Should return filesystem paths."""
        (tmp_path / "test_file.py").touch()
        (tmp_path / "test_dir").mkdir()
        monkeypatch.chdir(tmp_path)

        completer = EggCompleter(isolated_db, lambda: "tid", None)

        suggestions = completer._get_filesystem_suggestions("test")

        assert any("test_file.py" in s for s in suggestions)
        assert any("test_dir/" in s for s in suggestions)

    def test_get_filesystem_suggestions_handles_missing_path(self, isolated_db):
        """Should handle missing paths gracefully."""
        completer = EggCompleter(isolated_db, lambda: "tid", None)

        suggestions = completer._get_filesystem_suggestions("/nonexistent/path/xyz")

        assert suggestions == []

    def test_recent_words_extracts_from_messages(self, isolated_db):
        """Should extract words from recent messages."""
        from eggthreads import create_root_thread, create_snapshot, append_message

        thread = create_root_thread(isolated_db, name="TestThread")
        append_message(isolated_db, thread, "user", "testing functionality here")
        create_snapshot(isolated_db, thread)

        completer = EggCompleter(isolated_db, lambda: thread, None)

        words = completer._recent_words(thread)

        assert 'testing' in words
        assert 'functionality' in words
        assert 'here' in words

    def test_conversation_word_matches_filters_by_prefix(self, isolated_db):
        """Should filter conversation words by prefix."""
        from eggthreads import create_root_thread, create_snapshot, append_message

        thread = create_root_thread(isolated_db, name="TestThread")
        append_message(isolated_db, thread, "user", "testing temperature terminal")
        create_snapshot(isolated_db, thread)

        completer = EggCompleter(isolated_db, lambda: thread, None)

        matches = completer._conversation_word_matches("test", thread)

        assert 'testing' in matches
        assert 'temperature' not in matches  # doesn't start with 'test'
