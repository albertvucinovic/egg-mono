"""Reusable named argument parser for commands.

Supports both positional and named arguments in command strings.

Examples:
    >>> parse_args("thread_id wait=30")
    ParsedArgs(positional=['thread_id'], named={'wait': '30'})

    >>> parse_args('name="My Thread" msg_id=abc123')
    ParsedArgs(positional=[], named={'name': 'My Thread', 'msg_id': 'abc123'})

    >>> parse_args("arg1 arg2 key=value")
    ParsedArgs(positional=['arg1', 'arg2'], named={'key': 'value'})
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ParsedArgs:
    """Result of parsing command arguments."""
    positional: List[str]
    named: Dict[str, str]

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a named argument value."""
        return self.named.get(key, default)

    def get_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        """Get a named argument as integer."""
        val = self.named.get(key)
        if val is None:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    def get_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        """Get a named argument as float."""
        val = self.named.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except ValueError:
            return default

    def positional_or(self, index: int, default: Optional[str] = None) -> Optional[str]:
        """Get positional argument at index, or default if not present."""
        if index < len(self.positional):
            return self.positional[index]
        return default


def parse_args(arg_string: str) -> ParsedArgs:
    """Parse command arguments supporting both positional and named args.

    Supports:
    - Positional arguments: arg1 arg2
    - Named arguments: key=value key2="value with spaces" key3='single quoted'
    - Mixed: arg1 key=value arg2

    Named arguments can have:
    - Unquoted values: key=value (no spaces allowed in value)
    - Double-quoted values: key="value with spaces"
    - Single-quoted values: key='value with spaces'

    Args:
        arg_string: The argument string to parse (without the command name)

    Returns:
        ParsedArgs with positional and named arguments separated
    """
    positional: List[str] = []
    named: Dict[str, str] = {}

    if not arg_string or not arg_string.strip():
        return ParsedArgs(positional=[], named={})

    # Track position in string to avoid re-matching
    remaining = arg_string.strip()

    while remaining:
        remaining = remaining.lstrip()
        if not remaining:
            break

        # Try to match named argument: key=value or key="value" or key='value'
        named_match = re.match(
            r'''(\w+)=(?:"([^"]*?)"|'([^']*?)'|(\S+))''',
            remaining
        )
        if named_match:
            key = named_match.group(1)
            # Value is in group 2 (double-quoted), 3 (single-quoted), or 4 (unquoted)
            value = named_match.group(2)
            if value is None:
                value = named_match.group(3)
            if value is None:
                value = named_match.group(4)
            if value is None:
                value = ""
            named[key] = value
            remaining = remaining[named_match.end():]
            continue

        # Try to match positional argument (non-whitespace, not starting with key=)
        pos_match = re.match(r'(\S+)', remaining)
        if pos_match:
            val = pos_match.group(1)
            # Make sure it's not a malformed named arg
            if '=' not in val:
                positional.append(val)
            remaining = remaining[pos_match.end():]
            continue

        # Shouldn't reach here, but break to avoid infinite loop
        break

    return ParsedArgs(positional=positional, named=named)
