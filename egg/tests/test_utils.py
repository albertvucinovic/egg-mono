"""Tests for utils.py utility functions."""
from __future__ import annotations

import json
import sys

import pytest

from egg.utils import (
    get_system_prompt,
    snapshot_messages,
    get_subtree,
    looks_markdown,
    shorten_output_preview,
    read_clipboard,
    restore_tty,
    SYSTEM_PROMPT_PATH,
)


class TestGetSystemPrompt:
    """Tests for get_system_prompt()."""

    def test_reads_system_prompt_file(self, tmp_path, monkeypatch):
        """Should read content from systemPrompt file when it exists."""
        # Create a temporary system prompt file
        prompt_file = tmp_path / "systemPrompt"
        prompt_file.write_text("Custom system prompt for testing.")

        # Patch SYSTEM_PROMPT_PATH to use our temp file
        import egg.utils as utils
        monkeypatch.setattr(utils, "SYSTEM_PROMPT_PATH", prompt_file)

        result = get_system_prompt()
        assert result == "Custom system prompt for testing."

    def test_strips_whitespace(self, tmp_path, monkeypatch):
        """Should strip leading/trailing whitespace from prompt."""
        prompt_file = tmp_path / "systemPrompt"
        prompt_file.write_text("  \n  Prompt with whitespace  \n  ")

        import egg.utils as utils
        monkeypatch.setattr(utils, "SYSTEM_PROMPT_PATH", prompt_file)

        result = get_system_prompt()
        assert result == "Prompt with whitespace"

    def test_returns_default_on_missing_file(self, tmp_path, monkeypatch):
        """Should return default prompt when file is missing."""
        import egg.utils as utils
        monkeypatch.setattr(utils, "SYSTEM_PROMPT_PATH", tmp_path / "nonexistent")

        result = get_system_prompt()
        assert result == "You are a helpful assistant."

    def test_returns_default_on_read_error(self, tmp_path, monkeypatch):
        """Should return default prompt on any read error."""
        # Create a directory instead of file to cause read error
        prompt_dir = tmp_path / "systemPrompt"
        prompt_dir.mkdir()

        import egg.utils as utils
        monkeypatch.setattr(utils, "SYSTEM_PROMPT_PATH", prompt_dir)

        result = get_system_prompt()
        assert result == "You are a helpful assistant."


class TestSnapshotMessages:
    """Tests for snapshot_messages()."""

    def test_extracts_messages_from_snapshot(self, thread_with_messages):
        """Should extract messages list from thread snapshot."""
        db, tid = thread_with_messages

        msgs = snapshot_messages(db, tid)

        assert len(msgs) == 3
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"

    def test_returns_empty_on_no_snapshot(self, isolated_db):
        """Should return empty list when thread has no snapshot."""
        from eggthreads import create_root_thread

        tid = create_root_thread(isolated_db, name="NoSnapshot")
        # Don't create snapshot

        msgs = snapshot_messages(isolated_db, tid)
        assert msgs == []

    def test_returns_empty_on_invalid_thread(self, isolated_db):
        """Should return empty list for non-existent thread."""
        msgs = snapshot_messages(isolated_db, "nonexistent_thread_id")
        assert msgs == []

    def test_returns_empty_on_malformed_json(self, isolated_db, monkeypatch):
        """Should return empty list on malformed snapshot JSON."""
        from eggthreads import create_root_thread

        tid = create_root_thread(isolated_db, name="MalformedSnapshot")

        # Manually set malformed snapshot
        isolated_db.conn.execute(
            "UPDATE threads SET snapshot_json = ? WHERE thread_id = ?",
            ("not valid json{{{", tid)
        )
        isolated_db.conn.commit()

        msgs = snapshot_messages(isolated_db, tid)
        assert msgs == []


class TestGetSubtree:
    """Tests for get_subtree()."""

    def test_returns_all_descendants(self, isolated_db):
        """Should return all child thread IDs in subtree."""
        from eggthreads import create_root_thread, create_child_thread

        root = create_root_thread(isolated_db, name="Root")
        child1 = create_child_thread(isolated_db, root, name="Child1")
        child2 = create_child_thread(isolated_db, root, name="Child2")

        subtree = get_subtree(isolated_db, root)

        assert child1 in subtree
        assert child2 in subtree

    def test_excludes_root(self, isolated_db):
        """Should not include root ID in result."""
        from eggthreads import create_root_thread, create_child_thread

        root = create_root_thread(isolated_db, name="Root")
        create_child_thread(isolated_db, root, name="Child1")

        subtree = get_subtree(isolated_db, root)

        assert root not in subtree

    def test_includes_grandchildren(self, isolated_db):
        """Should include nested descendants."""
        from eggthreads import create_root_thread, create_child_thread

        root = create_root_thread(isolated_db, name="Root")
        child = create_child_thread(isolated_db, root, name="Child")
        grandchild = create_child_thread(isolated_db, child, name="Grandchild")

        subtree = get_subtree(isolated_db, root)

        assert child in subtree
        assert grandchild in subtree

    def test_returns_empty_for_no_children(self, isolated_db):
        """Should return empty list for thread with no children."""
        from eggthreads import create_root_thread

        root = create_root_thread(isolated_db, name="Lonely")

        subtree = get_subtree(isolated_db, root)

        assert subtree == []


class TestLooksMarkdown:
    """Tests for looks_markdown()."""

    def test_detects_code_blocks(self):
        """Should return True for content with code blocks."""
        content = "Here is some code:\n```python\nprint('hello')\n```"
        assert looks_markdown(content) is True

    def test_detects_headers(self):
        """Should return True for # headers."""
        content = "# Main Title\n\nSome content here."
        assert looks_markdown(content) is True

    def test_detects_subheaders(self):
        """Should return True for ## subheaders."""
        content = "## Section\n\nMore content."
        assert looks_markdown(content) is True

    def test_detects_bullet_lists_asterisk(self):
        """Should return True for * bullet lists."""
        content = "Items:\n* First item\n* Second item"
        assert looks_markdown(content) is True

    def test_detects_bullet_lists_dash(self):
        """Should return True for - bullet lists."""
        content = "Items:\n- First item\n- Second item"
        assert looks_markdown(content) is True

    def test_detects_blockquotes(self):
        """Should return True for > blockquotes."""
        content = "Someone said:\n> This is a quote\n> Over multiple lines"
        assert looks_markdown(content) is True

    def test_detects_inline_code_with_newlines(self):
        """Should return True for inline `code` with multiple lines."""
        # Single backtick indicator counts as 1, needs 2+ newlines
        content = "Line 1\nUse `some_function()` to do it.\nLine 3"
        assert looks_markdown(content) is True

    def test_returns_false_for_plain_text(self):
        """Should return False for plain text without markdown."""
        content = "This is just plain text without any formatting."
        assert looks_markdown(content) is False

    def test_returns_false_for_empty_string(self):
        """Should return False for empty string."""
        assert looks_markdown("") is False

    def test_returns_false_for_none(self):
        """Should return False for None."""
        assert looks_markdown(None) is False

    def test_single_indicator_with_newlines(self):
        """Should return True for single indicator with multiple lines."""
        content = "Line 1\nLine 2\n* Item"
        assert looks_markdown(content) is True

    def test_single_indicator_no_newlines(self):
        """Should return False for single indicator without newlines."""
        content = "Just a * single asterisk"
        assert looks_markdown(content) is False


class TestShortenOutputPreview:
    """Tests for shorten_output_preview()."""

    def test_preserves_short_output(self):
        """Should not modify output within limits."""
        short_text = "This is short output."
        result = shorten_output_preview(short_text)
        assert result == short_text

    def test_truncates_by_line_count(self):
        """Should truncate output exceeding max_lines."""
        long_text = "\n".join(f"Line {i}" for i in range(300))
        result = shorten_output_preview(long_text, max_lines=10, max_chars=10000)

        lines = result.splitlines()
        # Should have 10 lines plus truncation notice
        assert len(lines) <= 13  # 10 content + blank + notice
        assert "truncated" in result.lower()

    def test_truncates_by_char_count(self):
        """Should truncate output exceeding max_chars."""
        long_text = "x" * 10000
        result = shorten_output_preview(long_text, max_lines=1000, max_chars=100)

        assert len(result) < len(long_text)
        assert "truncated" in result.lower()

    def test_adds_truncation_notice(self):
        """Should add notice when truncated."""
        long_text = "\n".join(f"Line {i}" for i in range(500))
        result = shorten_output_preview(long_text, max_lines=10)

        assert "...[output truncated for preview]..." in result

    def test_handles_empty_string(self):
        """Should return empty string for empty input."""
        assert shorten_output_preview("") == ""

    def test_handles_none(self):
        """Should return empty string for None."""
        assert shorten_output_preview(None) == ""

    def test_handles_non_string(self):
        """Should return empty string for non-string input."""
        assert shorten_output_preview(123) == ""


class TestReadClipboard:
    """Tests for read_clipboard()."""

    def test_uses_pyperclip_when_available(self, monkeypatch):
        """Should use pyperclip.paste() when installed."""
        # Create a mock pyperclip module
        class MockPyperclip:
            @staticmethod
            def paste():
                return "clipboard content from pyperclip"

        monkeypatch.setitem(sys.modules, 'pyperclip', MockPyperclip())

        # Need to reimport to pick up mocked module
        import egg.utils as utils
        import importlib
        importlib.reload(utils)

        result = utils.read_clipboard()
        assert result == "clipboard content from pyperclip"

    def test_returns_none_on_all_failures(self, monkeypatch):
        """Should return None when all methods fail."""
        # Remove pyperclip from modules
        monkeypatch.setitem(sys.modules, 'pyperclip', None)

        # Mock subprocess.run to always fail
        def mock_run(*args, **kwargs):
            raise FileNotFoundError("No clipboard command")

        monkeypatch.setattr("subprocess.run", mock_run)

        import egg.utils as utils
        import importlib
        importlib.reload(utils)

        result = utils.read_clipboard()
        assert result is None


class TestRestoreTty:
    """Tests for restore_tty()."""

    def test_handles_no_termios(self, monkeypatch):
        """Should handle gracefully when termios is not available."""
        # Remove termios from modules to simulate unavailability
        monkeypatch.setitem(sys.modules, 'termios', None)

        # Should not raise
        restore_tty()

    def test_handles_non_tty_stdin(self, monkeypatch):
        """Should handle gracefully when stdin is not a TTY."""
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)

        # Should not raise
        restore_tty()

    def test_handles_termios_errors(self, monkeypatch):
        """Should handle termios errors gracefully."""
        class MockTermios:
            ECHO = 8
            ICANON = 2
            TCSADRAIN = 1

            @staticmethod
            def tcgetattr(fd):
                raise OSError("Mock termios error")

            @staticmethod
            def tcsetattr(fd, when, attrs):
                pass

        monkeypatch.setitem(sys.modules, 'termios', MockTermios())
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(sys.stdin, 'fileno', lambda: 0)

        # Should not raise
        restore_tty()
