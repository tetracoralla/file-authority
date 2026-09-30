"""Path validation and error plumbing for workspace file authority.

The check-then-use machinery lives in :mod:`file_authority.anchored`; every
function there anchors the granted object with a directory descriptor or an
open file handle before handing it to a caller, so a path component swapped
between check and use cannot redirect actual reads or writes outside the
granted workspace.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

MAX_PATH_CHARS = 4096

_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: Builds the exception a check fails with; the caller may substitute a
#: factory returning its own error type so public failures stay unchanged.
#: The anchored machinery always raises the built exception itself.
ErrorFactory = Callable[[str, str], BaseException]


class FileAuthorityError(Exception):
    """Neutral error with a stable code; products translate to their own type."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_error(code: str, message: str) -> FileAuthorityError:
    return FileAuthorityError(code, message)


def relative_parts(raw: str, *, error: ErrorFactory = default_error) -> tuple[str, ...]:
    """Validate a workspace-relative path string and return its clean parts.

    Rejects non-strings, empty strings, NUL bytes, over-length strings,
    URI-like scheme prefixes (``https:``, ``file:``, ``c:`` …), home-relative
    paths, absolute paths, parent traversal, and paths that name no file.
    ``""`` and ``.`` components are dropped.
    """

    if not isinstance(raw, str) or not raw or "\x00" in raw or len(raw) > MAX_PATH_CHARS:
        raise error("INVALID_INPUT", "path must be a non-empty bounded string")
    if _SCHEME_PREFIX.match(raw) or raw.startswith("~"):
        raise error("PATH_FORBIDDEN", "URI-like and home-relative paths are forbidden")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise error("PATH_FORBIDDEN", "path must stay inside the workspace")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts:
        raise error("INVALID_INPUT", "path must identify a file")
    return parts
