"""Anchored file authority: checks and use refer to the same granted object.

Every resolver here opens the target through its parent directory
descriptor with ``O_NOFOLLOW`` per component, so the object a caller later
reads, writes, or publishes is the object that was verified — a component
renamed or replaced by a symlink after the check cannot redirect access
outside the granted workspace. Consumers receive open handles (file
descriptors) or anchored directory descriptors, never bare re-openable
paths; :meth:`AnchoredDirectory.materialized_path` is the explicit, verified
escape hatch for external programs that can only take a path.
"""

from __future__ import annotations

import ctypes
import errno
import io
import os
import secrets
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, NamedTuple

from .core import ErrorFactory, default_error, relative_parts

_CHUNK = 1024 * 1024

_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NONBLOCK
_DIRECTORY_FLAGS = _OPEN_FLAGS | os.O_DIRECTORY
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _is_symlink(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode)


def _child_name(name: str, *, error: ErrorFactory = default_error) -> str:
    if name in {".", ".."}:
        raise error("PATH_FORBIDDEN", "child name must be one workspace-relative component")
    parts = relative_parts(name, error=error)
    if len(parts) != 1 or parts[0] != name:
        raise error("PATH_FORBIDDEN", "child name must be one workspace-relative component")
    return name


class AnchoredDirectory:
    """A verified directory held open by descriptor.

    Child names opened through :attr:`fd` cannot be redirected by renaming or
    replacing any ancestor of the directory. Close releases the descriptor.
    """

    def __init__(self, fd: int, remembered_path: str | None = None) -> None:
        self.fd = fd
        self._remembered_path = remembered_path

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> AnchoredDirectory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown best effort
        with suppress(OSError):
            self.close()

    def materialized_path(self, *, error: ErrorFactory = default_error) -> str:
        """Return a real path that currently resolves to this directory.

        On Linux the live descriptor path is read from ``/proc/self/fd``;
        otherwise the path remembered from anchoring is used. Either way the
        binding is re-verified by inode before the path is returned, so a
        component swapped or renamed after anchoring fails safely instead of
        handing out a path that now leads elsewhere.
        """

        if self.fd < 0:
            raise error("PATH_FORBIDDEN", "anchored directory is already closed")
        candidates: list[str] = []
        proc_path = f"/proc/self/fd/{self.fd}"
        if os.path.exists(proc_path):
            with suppress(OSError):
                candidates.append(os.readlink(proc_path))
        if self._remembered_path:
            candidates.append(self._remembered_path)
        anchored = os.fstat(self.fd)
        for path in candidates:
            if not path or path.endswith(" (deleted)"):
                continue
            try:
                bound = os.stat(path)
            except OSError:
                continue
            if bound.st_ino == anchored.st_ino and bound.st_dev == anchored.st_dev:
                return path
        raise error(
            "PATH_FORBIDDEN", "anchored directory is no longer reachable by a verified path"
        )

    def materialized_path_quiet(self) -> str | None:
        """Best-effort current path for bookkeeping; never raises."""

        try:
            return self.materialized_path()
        except Exception:
            proc_path = f"/proc/self/fd/{self.fd}"
            if os.path.exists(proc_path):
                try:
                    return os.readlink(proc_path)
                except OSError:
                    return None
            return self._remembered_path

    def writable(self) -> bool:
        """Effective write permission on this directory (fstat-based)."""

        info = os.fstat(self.fd)
        if os.geteuid() == 0:
            return True
        mode = info.st_mode
        if os.geteuid() == info.st_uid:
            return bool(mode & 0o200)
        if os.getegid() == info.st_gid:
            return bool(mode & 0o020)
        return bool(mode & 0o002)

    def child_status(self, name: str) -> tuple[str, int]:
        """Classify a direct child without following it: missing/symlink/file/other."""

        _child_name(name)
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing", 0
        if _is_symlink(info):
            return "symlink", 0
        if stat.S_ISREG(info.st_mode):
            return "file", info.st_size
        return "other", info.st_size


def anchor_root(root: Path, *, error: ErrorFactory = default_error) -> AnchoredDirectory:
    """Open the granted workspace root as an anchored directory.

    The root is the host's grant, not agent input: it is resolved strictly
    (following the grant, e.g. ``/tmp`` → ``/private/tmp``) before opening.
    Everything below it is walked without following symlinks.
    """

    try:
        resolved = Path(root).resolve(strict=True)
    except OSError as failure:
        raise error("PATH_FORBIDDEN", "workspace root is unavailable") from failure
    try:
        fd = os.open(resolved, _DIRECTORY_FLAGS)
    except OSError as failure:
        raise error("PATH_FORBIDDEN", "workspace root must be a real directory") from failure
    return AnchoredDirectory(fd, remembered_path=str(resolved))


def _open_directory_component(
    directory: AnchoredDirectory,
    part: str,
    *,
    missing: tuple[str, str] = ("SOURCE_NOT_FOUND", "workspace component does not exist"),
    error: ErrorFactory = default_error,
) -> AnchoredDirectory:
    parent_path = directory.materialized_path_quiet()
    try:
        fd = os.open(part, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=directory.fd)
    except FileNotFoundError as failure:
        raise error(missing[0], missing[1]) from failure
    except NotADirectoryError as failure:
        raise error("PATH_FORBIDDEN", "workspace path component is not a directory") from failure
    except OSError as failure:
        if failure.errno == errno.ELOOP:
            raise error("PATH_FORBIDDEN", "symlink path components are forbidden") from failure
        raise error("PATH_FORBIDDEN", "workspace path component is unavailable") from failure
    remembered = f"{parent_path}/{part}" if parent_path else None
    return AnchoredDirectory(fd, remembered_path=remembered)


class _SnapshotReader(io.RawIOBase):
    """Read-only view over an open descriptor capped at the verified snapshot.

    Reads at or past the snapshot size return EOF, so in-place growth after
    the check cannot inflate what a consumer reads through this handle.
    Closing the view never closes the underlying descriptor.
    """

    def __init__(self, fd: int, limit: int) -> None:
        self._file = io.FileIO(fd, "r", closefd=False)
        self._limit = limit

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        position = self._file.tell()
        remaining = self._limit - position
        if remaining <= 0:
            return 0
        view = memoryview(buffer)
        if len(view) > remaining:
            view = view[:remaining]
        return self._file.readinto(view)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()


class OpenInput:
    """A regular-file input held open without following symlinks.

    ``size`` is the snapshot taken when the file was opened; library reads are
    bounded by that snapshot, so in-place growth after the check cannot
    inflate those reads beyond the verified budget. Reads through
    the handle always target the opened inode even if the path is replaced.
    """

    def __init__(self, fd: int, size: int, name: str) -> None:
        self.fd = fd
        self.size = size
        self.name = name

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> OpenInput:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown best effort
        with suppress(OSError):
            self.close()

    def file_object(self) -> io.RawIOBase:
        """A readable binary file object capped at the snapshot size.

        Reads never see bytes beyond the verified snapshot even if the file
        grows in place afterwards; closing the object keeps the fd open.
        """

        if self.fd < 0:
            raise ValueError("input is already closed")
        return _SnapshotReader(self.fd, self.size)

    def read_bytes(self, *, limit: int | None = None) -> bytes:
        """Read at most the snapshot size (or ``limit``) bytes from this handle."""

        budget = self.size if limit is None else min(self.size, limit)
        chunks: list[bytes] = []
        remaining = budget
        while remaining > 0:
            block = os.read(self.fd, min(_CHUNK, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def copy_to_path(
        self, path: Path, *, max_bytes: int | None = None,
        check: Callable[[], object] | None = None,
    ) -> int:
        """Stream this handle into a caller-owned path (task-private staging).

        Returns the number of bytes copied; the copy never exceeds the
        verified snapshot (or ``max_bytes`` if smaller).
        """

        budget = self.size if max_bytes is None else min(self.size, max_bytes)
        copied = 0
        with open(path, "wb") as sink:
            while copied < budget:
                if check is not None:
                    check()
                block = os.read(self.fd, min(_CHUNK, budget - copied))
                if not block:
                    break
                sink.write(block)
                copied += len(block)
            sink.flush()
            os.fsync(sink.fileno())
        return copied

    def copy_into(
        self,
        directory: AnchoredDirectory,
        name: str,
        *,
        max_bytes: int | None = None,
        check: Callable[[], object] | None = None,
        error: ErrorFactory = default_error,
    ) -> None:
        """Stream this handle into a new exclusive file inside an anchored directory."""

        _child_name(name, error=error)
        budget = self.size if max_bytes is None else min(self.size, max_bytes)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            target = os.open(name, flags, 0o600, dir_fd=directory.fd)
        except OSError as failure:
            raise error("OUTPUT_INVALID", "stabilized copy could not be created") from failure
        copied = 0
        try:
            while copied < budget:
                if check is not None:
                    check()
                block = os.read(self.fd, min(_CHUNK, budget - copied))
                if not block:
                    break
                offset = 0
                while offset < len(block):
                    offset += os.write(target, block[offset:])
                copied += len(block)
            os.fsync(target)
        except BaseException as failure:
            with suppress(OSError):
                os.unlink(name, dir_fd=directory.fd)
            if isinstance(failure, OSError):
                raise error("OUTPUT_INVALID", "stabilized copy could not be written") from failure
            raise
        finally:
            os.close(target)

    def subprocess_reference(self) -> str:
        """A path reference to this handle for direct subprocess use.

        The caller must pass the descriptor through (``pass_fds=[input.fd]``)
        and keep this object open for the lifetime of the subprocess. There
        is no fallback: platforms without ``/dev/fd`` raise.
        """

        if self.fd < 0:
            raise ValueError("input is already closed")
        if os.path.exists("/dev/fd"):
            return f"/dev/fd/{self.fd}"
        if os.path.exists("/proc/self/fd"):
            return f"/proc/self/fd/{self.fd}"
        raise OSError(
            errno.ENOTSUP, "no /dev/fd or /proc/self/fd support for anchored subprocess input"
        )


def open_input_file(
    root: Path | AnchoredDirectory,
    raw: str,
    *,
    max_bytes: int | None = None,
    error: ErrorFactory = default_error,
) -> OpenInput:
    """Open an existing regular file under the granted root without following symlinks."""

    parts = relative_parts(raw, error=error)
    given = root if isinstance(root, AnchoredDirectory) else None
    current = (
        AnchoredDirectory(os.dup(given.fd), given.materialized_path_quiet())
        if given is not None else anchor_root(root, error=error)
    )
    owned = [current]
    try:
        for part in parts[:-1]:
            child = _open_directory_component(current, part, error=error)
            for stale in owned:
                stale.close()
            owned = [child]
            current = child
        name = parts[-1]
        try:
            fd = os.open(name, _OPEN_FLAGS | _NOFOLLOW, dir_fd=current.fd)
        except FileNotFoundError as failure:
            raise error("SOURCE_NOT_FOUND", "source file does not exist") from failure
        except NotADirectoryError as failure:
            raise error("PATH_FORBIDDEN", "source must be a regular file") from failure
        except OSError as failure:
            if failure.errno == errno.ELOOP:
                raise error("PATH_FORBIDDEN", "symlink path components are forbidden") from failure
            raise error("PATH_FORBIDDEN", "source path is unavailable") from failure
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise error("PATH_FORBIDDEN", "source must be a regular file")
        if max_bytes is not None and info.st_size > max_bytes:
            os.close(fd)
            raise error("LIMIT_EXCEEDED", f"source exceeds {max_bytes} bytes")
        return OpenInput(fd=fd, size=info.st_size, name=name)
    finally:
        for directory in owned:
            directory.close()


class OutputFileSlot:
    """An output file target anchored in its verified parent directory."""

    def __init__(self, directory: AnchoredDirectory, name: str, exists: bool) -> None:
        self.directory = directory
        self.name = name
        self.exists = exists


def anchor_output_file(
    root: Path | AnchoredDirectory,
    raw: str,
    *,
    require_writable: bool = False,
    error: ErrorFactory = default_error,
) -> OutputFileSlot:
    """Anchor a workspace-relative output file whose parents already exist."""

    parts = relative_parts(raw, error=error)
    given = root if isinstance(root, AnchoredDirectory) else None
    current = (
        AnchoredDirectory(os.dup(given.fd), given.materialized_path_quiet())
        if given is not None else anchor_root(root, error=error)
    )
    owned = [current]
    try:
        for part in parts[:-1]:
            child = _open_directory_component(
                current,
                part,
                missing=("OUTPUT_INVALID", "output parent directories must already exist"),
                error=error,
            )
            for stale in owned:
                stale.close()
            owned = [child]
            current = child
        if require_writable and not current.writable():
            raise error("OUTPUT_INVALID", "output directory is not writable")
        name = parts[-1]
        kind, _size = current.child_status(name)
        if kind in {"symlink", "other"}:
            raise error("PATH_FORBIDDEN", "output target must be a regular file or new path")
        slot = OutputFileSlot(directory=current, name=name, exists=kind == "file")
        owned.clear()  # ownership transfers to the slot
        return slot
    finally:
        for directory in owned:
            directory.close()


class OutputDirectorySlot:
    """An output directory name anchored in its verified parent directory.

    ``existed`` records whether the name already resolved to a real writable
    directory. A missing name is intentionally NOT created here: publishing
    can place a rendered directory into the name with one rename, which the
    owning product decides. ``open()`` yields the final directory descriptor
    once it exists.
    """

    def __init__(
        self,
        container: AnchoredDirectory,
        name: str,
        directory: AnchoredDirectory | None,
        existed: bool,
    ) -> None:
        self.container = container
        self.name = name
        self._directory = directory
        self.existed = existed

    @property
    def directory(self) -> AnchoredDirectory | None:
        return self._directory

    def open(self, *, error: ErrorFactory = default_error) -> AnchoredDirectory:
        """Open (or re-open) the final directory by name through the container."""

        if self._directory is not None and self._directory.fd >= 0:
            return self._directory
        directory = _open_directory_component(
            self.container,
            self.name,
            missing=("OUTPUT_INVALID", "output directory does not exist"),
            error=error,
        )
        self._directory = directory
        return directory

    def close(self) -> None:
        if self._directory is not None:
            self._directory.close()
        self.container.close()


def anchor_output_directory(
    root: Path | AnchoredDirectory,
    raw: str,
    *,
    error: ErrorFactory = default_error,
) -> OutputDirectorySlot:
    """Anchor a workspace-relative output directory without creating it.

    All existing components must be real directories. A missing final name is
    reported with ``existed=False`` together with its verified writable
    container, so publication can place a directory into the name atomically;
    missing non-final components are refused.
    """

    parts = relative_parts(raw, error=error)
    given = root if isinstance(root, AnchoredDirectory) else None
    current = (
        AnchoredDirectory(os.dup(given.fd), given.materialized_path_quiet())
        if given is not None else anchor_root(root, error=error)
    )
    owned = [current]
    final_fd: int | None = None
    transferred = False
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                fd = os.open(part, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=current.fd)
            except FileNotFoundError as failure:
                if not final:
                    raise error(
                        "OUTPUT_INVALID", "only the final output directory may be created"
                    ) from failure
                if not current.writable():
                    raise error(
                        "OUTPUT_INVALID", "output directory parent is not writable"
                    ) from failure
                break
            except NotADirectoryError as failure:
                raise error(
                    "PATH_FORBIDDEN", "output path components must be real directories"
                ) from failure
            except OSError as failure:
                if failure.errno == errno.ELOOP:
                    raise error(
                        "PATH_FORBIDDEN", "symlink path components are forbidden"
                    ) from failure
                raise error("PATH_FORBIDDEN", "output path is unavailable") from failure
            if final:
                final_fd = fd
                break
            child = AnchoredDirectory(fd)
            for stale in owned:
                stale.close()
            owned = [child]
            current = child
        if final_fd is None:
            slot = OutputDirectorySlot(
                container=current, name=parts[-1], directory=None, existed=False
            )
        else:
            final_dir = AnchoredDirectory(final_fd)
            if not final_dir.writable():
                final_dir.close()
                final_fd = None
                raise error("OUTPUT_INVALID", "output directory is not writable")
            slot = OutputDirectorySlot(
                container=current, name=parts[-1], directory=final_dir, existed=True
            )
        owned.clear()  # container ownership transfers to the slot
        transferred = True
        return slot
    finally:
        for directory in owned:
            directory.close()
        if final_fd is not None and not transferred:
            os.close(final_fd)


class StagedFile:
    """An exclusive staging file created inside an anchored directory."""

    def __init__(self, directory: AnchoredDirectory, name: str, fd: int) -> None:
        self.directory = directory
        self.name = name
        self.fd = fd

    def file_object(self) -> io.FileIO:
        """A writable binary file object on the staging handle (keeps the fd)."""

        if self.fd < 0:
            raise ValueError("staging file is already consumed")
        return io.FileIO(self.fd, "w", closefd=False)

    def size(self) -> int:
        return os.fstat(self.fd).st_size

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def create_staging(
    directory: AnchoredDirectory, *, prefix: str, error: ErrorFactory = default_error
) -> StagedFile:
    """Create an exclusive staging file visible only inside the anchored directory."""

    _child_name(prefix, error=error)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(8):
        name = f".{prefix}.{secrets.token_hex(8)}"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory.fd)
            return StagedFile(directory=directory, name=name, fd=fd)
        except FileExistsError:
            continue
        except OSError as failure:
            raise error("OUTPUT_INVALID", "staging file could not be created") from failure
    raise error("OUTPUT_INVALID", "staging file could not be created")


class PublishOutcome(NamedTuple):
    """Honest publication result: what was published, and cleanup state."""

    published: bool
    staging_removed: bool


def _unlink_quietly(staging: StagedFile) -> bool:
    try:
        os.unlink(staging.name, dir_fd=staging.directory.fd)
        return True
    except OSError:
        return False


def publish_file(
    staging: StagedFile,
    target: OutputFileSlot,
    *,
    overwrite: bool,
    error: ErrorFactory = default_error,
) -> PublishOutcome:
    """Publish one staged file into an anchored output slot.

    Without ``overwrite`` the output name is claimed by hard-link creation,
    which is atomic against concurrent writers; a racing claim fails with
    ``OUTPUT_EXISTS`` and nothing was published. A failure to remove the
    staging file *after* a successful link is reported through the outcome
    (``published=True, staging_removed=False``) instead of being misreported
    as an unpublished failure. With ``overwrite`` the staging name is renamed
    over the target in one step.
    """

    _child_name(staging.name, error=error)
    _child_name(target.name, error=error)
    try:
        if overwrite:
            os.rename(
                staging.name,
                target.name,
                src_dir_fd=staging.directory.fd,
                dst_dir_fd=target.directory.fd,
            )
            staging.close()
            return PublishOutcome(published=True, staging_removed=True)
        os.link(
            staging.name,
            target.name,
            src_dir_fd=staging.directory.fd,
            dst_dir_fd=target.directory.fd,
            follow_symlinks=False,
        )
    except FileExistsError as failure:
        removed = _unlink_quietly(staging)
        staging.close()
        message = "output was created concurrently"
        if not removed:
            message += "; removing the staging file also failed (output untouched)"
        raise error("OUTPUT_EXISTS", message) from failure
    except OSError as failure:
        removed = _unlink_quietly(staging)
        staging.close()
        message = "output could not be published"
        if not removed:
            message += "; removing the staging file also failed"
        raise error("OUTPUT_INVALID", message) from failure
    staging.close()
    if _unlink_quietly(staging):
        return PublishOutcome(published=True, staging_removed=True)
    return PublishOutcome(published=True, staging_removed=False)


def move_into(
    source: AnchoredDirectory,
    source_name: str,
    target: AnchoredDirectory,
    target_name: str,
    *,
    overwrite: bool,
    error: ErrorFactory = default_error,
) -> PublishOutcome:
    """Move one name between anchored directories (renameat / linkat+unlink).

    A building block for product-owned multi-file transactions such as
    Timeline's publication with backup and rollback.
    """

    _child_name(source_name, error=error)
    _child_name(target_name, error=error)
    try:
        if overwrite:
            os.rename(
                source_name,
                target_name,
                src_dir_fd=source.fd,
                dst_dir_fd=target.fd,
            )
            return PublishOutcome(True, True)
        else:
            os.link(
                source_name,
                target_name,
                src_dir_fd=source.fd,
                dst_dir_fd=target.fd,
                follow_symlinks=False,
            )
    except FileExistsError as failure:
        raise error("OUTPUT_EXISTS", "output was created concurrently") from failure
    except OSError as failure:
        raise error("OUTPUT_INVALID", "file could not be moved between anchored directories") from (
            failure
        )
    try:
        os.unlink(source_name, dir_fd=source.fd)
    except OSError:
        return PublishOutcome(True, False)
    return PublishOutcome(True, True)


def publish_directory(
    source: AnchoredDirectory,
    source_name: str,
    target: AnchoredDirectory,
    target_name: str,
    *,
    error: ErrorFactory = default_error,
) -> None:
    """Atomically claim a fresh output name without replacing a racing directory.

    macOS and Linux provide exclusive rename primitives. Other platforms fail
    explicitly instead of falling back to a check-then-rename race.
    """

    _child_name(source_name, error=error)
    _child_name(target_name, error=error)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(library, "renameatx_np", None)
        flags = 4  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        flags = 1  # RENAME_NOREPLACE
    else:
        rename = None
        flags = 0
    if rename is None:
        raise error("OUTPUT_INVALID", "exclusive directory publication is unsupported")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source.fd, os.fsencode(source_name), target.fd, os.fsencode(target_name), flags):
        code = ctypes.get_errno()
        failure = OSError(code, os.strerror(code))
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise error("OUTPUT_EXISTS", "output directory was created concurrently") from failure
        raise error("OUTPUT_INVALID", "output directory could not be published") from failure


def remove_child(directory: AnchoredDirectory, name: str) -> bool:
    """Unlink a direct child of an anchored directory; False if it was absent."""

    _child_name(name)
    try:
        os.unlink(name, dir_fd=directory.fd)
        return True
    except FileNotFoundError:
        return False


def read_child(
    directory: AnchoredDirectory,
    name: str,
    *,
    max_bytes: int | None = None,
    error: ErrorFactory = default_error,
) -> bytes:
    """Read a direct regular-file child without following symlinks."""

    _child_name(name, error=error)
    try:
        fd = os.open(name, _OPEN_FLAGS | _NOFOLLOW, dir_fd=directory.fd)
    except FileNotFoundError as failure:
        raise error("SOURCE_NOT_FOUND", "state file does not exist") from failure
    except OSError as failure:
        if failure.errno == errno.ELOOP:
            raise error("PATH_FORBIDDEN", "state path is a symlink") from failure
        raise error("PATH_FORBIDDEN", "state file is unavailable") from failure
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise error("PATH_FORBIDDEN", "state file must be a regular file")
        if max_bytes is not None and info.st_size > max_bytes:
            raise error("LIMIT_EXCEEDED", f"state file exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            block = os.read(fd, min(_CHUNK, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_child_exclusive(
    directory: AnchoredDirectory,
    name: str,
    payload: bytes,
    *,
    fsync: bool = True,
    error: ErrorFactory = default_error,
) -> bool:
    """Create a new file with ``payload``; False if the name already exists."""

    _child_name(name, error=error)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory.fd)
    except FileExistsError:
        return False
    except OSError as failure:
        if failure.errno == errno.ELOOP:
            raise error("PATH_FORBIDDEN", "state path is a symlink") from failure
        raise error("OUTPUT_INVALID", "state file could not be created") from failure
    try:
        _write_all(fd, payload)
        if fsync:
            os.fsync(fd)
    except OSError as failure:
        os.close(fd)
        remove_child(directory, name)
        raise error("OUTPUT_INVALID", "state file could not be written") from failure
    os.close(fd)
    return True


def replace_child_atomically(
    directory: AnchoredDirectory,
    name: str,
    payload: bytes,
    *,
    error: ErrorFactory = default_error,
) -> None:
    """Replace a direct child's content atomically via same-directory staging."""

    _child_name(name, error=error)
    staging = create_staging(directory, prefix=f"{name.lstrip('.')}.tmp", error=error)
    try:
        _write_all(staging.fd, payload)
        os.fsync(staging.fd)
        os.rename(staging.name, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
    except OSError as failure:
        _unlink_quietly(staging)
        raise error("OUTPUT_INVALID", "state file could not be replaced") from failure
    finally:
        staging.close()


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]
