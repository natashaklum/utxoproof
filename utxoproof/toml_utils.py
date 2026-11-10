"""Best-effort TOML loading for hand-edited user files.

Strict TOML remains the contract: comments, unknown keys and lists parse
normally. When an otherwise-valid manifest contains a stray legacy line (an
old YAML-style ``key: value``, a malformed list, duplicated key), this loader
skips the offending line, keeps the rest, and returns a warning per skipped
line. Required-field validation still runs afterwards, so a broken ``kind`` or
descriptor cannot disappear silently.
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

_LINE_ERROR = re.compile(r"at line (\d+), column (\d+)")


class TomlError(ValueError):
    """A TOML file could not be parsed even after skipping bad lines."""


def _error_line(exc: tomllib.TOMLDecodeError) -> int | None:
    line = getattr(exc, "lineno", None)
    if isinstance(line, int) and line > 0:
        return line
    match = _LINE_ERROR.search(str(exc))
    return int(match.group(1)) if match else None


def _last_content_line(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return index
    return None


def loads_lenient(text: str, source: str = "") -> tuple[dict[str, Any], list[str]]:
    """Parse TOML, dropping only the lines that make it invalid.

    Returns ``(data, warnings)``. If the file is so broken that nothing can be
    recovered, raises :class:`TomlError`. Callers still validate required keys,
    so dropping an essential line surfaces as the usual missing-field error.
    """
    try:
        return tomllib.loads(text), []
    except tomllib.TOMLDecodeError as exc:
        first_error = exc

    lines = text.splitlines()
    warnings: list[str] = []
    label = source or "<toml>"
    for _ in range(max(1, len(lines) + 1)):
        try:
            data = tomllib.loads("\n".join(lines))
        except tomllib.TOMLDecodeError as exc:
            line_no = _error_line(exc)
            index = line_no - 1 if line_no is not None else None
            if index is None or index < 0 or index >= len(lines):
                # "Unclosed array (at end of document)" points past the file;
                # drop the last content line and retry.
                index = _last_content_line(lines)
            if index is None:
                break
            stripped = lines[index].strip()
            if not stripped or stripped.startswith("#"):
                index = _last_content_line(lines)
                if index is None:
                    break
                stripped = lines[index].strip()
            reason = str(exc).split(" (at line", 1)[0]
            warnings.append(
                f"{label}: ignored invalid TOML line {index + 1}: {stripped!r} ({reason})"
            )
            # Blank rather than delete so later line numbers stay stable.
            lines[index] = ""
            continue
        return data, warnings
    raise TomlError(f"{label}: invalid TOML: {first_error}")
