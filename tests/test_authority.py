from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from file_authority import (
    FileAuthorityError,
    anchor_output_directory,
    anchor_output_file,
    anchor_root,
    create_staging,
    default_error,
    move_into,
    open_input_file,
    publish_directory,
    publish_file,
    read_child,
    relative_parts,
    remove_child,
    replace_child_atomically,
    write_child_exclusive,
)


class ToolError(Exception):
    """Mimics a product error type for factory-translation tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def tool_error(code: str, message: str) -> ToolError:
    return ToolError(code, message)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.wav").write_bytes(b"audio-bytes")
    (tmp_path / "in" / "nested").mkdir()
    (tmp_path / "in" / "nested" / "deep.png").write_bytes(b"png")
    (tmp_path / "out").mkdir()
    return tmp_path


def test_relative_parts_accepts_clean_relative_paths() -> None:
    assert relative_parts("in/a.wav") == ("in", "a.wav")


def test_relative_parts_rejects_escape_shapes() -> None:
    for raw in (
        "",
        "a\x00b",
        "x" * 4097,
        "https://example.com/a.wav",
        "file:///etc/passwd",
        "notes:v1.png",
        "c:\\windows\\system32",
        "~/a.wav",
        "/etc/passwd",
        "../outside.wav",
        "in/../../outside.wav",
        ".",
    ):
        with pytest.raises(FileAuthorityError) as caught:
            relative_parts(raw)
        assert caught.value.code in {"INVALID_INPUT", "PATH_FORBIDDEN"}


def test_error_factory_propagates_product_type_and_code() -> None:
    with pytest.raises(ToolError) as caught:
        open_input_file(Path("/nonexistent-root"), "a.wav", error=tool_error)
    assert caught.value.code == "PATH_FORBIDDEN"


def test_open_input_uses_the_verified_inode() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "in").mkdir()
        (root / "in" / "leaf.txt").write_bytes(b"original")
        with open_input_file(root, "in/leaf.txt") as source:
            # replace both the leaf and its parent directory after the check
            (root / "in" / "leaf.txt").unlink()
            (root / "in" / "leaf.txt").write_bytes(b"swapped-content")
            assert source.read_bytes() == b"original"
        # and via the ancestor
        (root / "in2").mkdir()
        (root / "in2" / "leaf.txt").write_bytes(b"original-2")
        with open_input_file(root, "in2/leaf.txt") as source:
            outside = root / "outside"
            outside.mkdir()
            (outside / "leaf.txt").write_bytes(b"outside-content")
            (root / "in2").rename(root / "in2-moved")
            (root / "in2").symlink_to(outside, target_is_directory=True)
            assert source.read_bytes() == b"original-2"


def test_open_input_rejects_symlinks_and_bad_shapes(workspace: Path) -> None:
    outside = workspace.parent / "outside-secret.wav"
    outside.write_bytes(b"secret")
    (workspace / "in" / "link.wav").symlink_to(outside)

    with open_input_file(workspace, "in/nested/deep.png") as source:
        assert source.name == "deep.png"
        assert source.size == 3

    for raw, code in [
        ("in/link.wav", "PATH_FORBIDDEN"),
        ("in/missing.wav", "SOURCE_NOT_FOUND"),
        ("in", "PATH_FORBIDDEN"),
        ("/etc/passwd", "PATH_FORBIDDEN"),
    ]:
        with pytest.raises(FileAuthorityError) as caught:
            open_input_file(workspace, raw)
        assert caught.value.code == code, raw


def test_open_input_size_is_a_stable_snapshot() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        leaf = root / "grow.txt"
        leaf.write_bytes(b"12345")
        with open_input_file(root, "grow.txt") as source:
            assert source.size == 5
            leaf.write_bytes(b"1" * 5000)  # grows in place after the check
            # the snapshot bounds what a caller can consume to the verified size;
            # in-place content mutation of the same inode remains visible by design
            consumed = source.read_bytes()
            assert len(consumed) == 5
        with pytest.raises(FileAuthorityError) as caught:
            open_input_file(root, "grow.txt", max_bytes=10)
        assert caught.value.code == "LIMIT_EXCEEDED"


def test_open_input_file_object_is_bounded_by_the_snapshot() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        leaf = root / "grow.bin"
        leaf.write_bytes(b"A" * 16)
        with open_input_file(root, "grow.bin") as source:
            leaf.write_bytes(b"A" * 16 + b"B" * 48)  # grows in place after the check
            handle = source.file_object()
            assert handle.read() == b"A" * 16  # growth is invisible
            handle.seek(0)
            assert handle.read(4) == b"A" * 4
            handle.seek(8)
            assert handle.read(100) == b"A" * 8  # clamped at the snapshot end
            handle.seek(0)
            assert handle.read(0) == b""
            handle.close()
            # closing the view keeps the underlying descriptor usable
            assert source.read_bytes() == b"A" * 16


def test_open_input_subprocess_reference_feeds_a_real_subprocess() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "data.bin").write_bytes(b"fd-payload")
        with open_input_file(root, "data.bin") as source:
            reference = source.subprocess_reference()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(open(sys.argv[1],'rb').read())",
                    reference,
                ],
                capture_output=True,
                pass_fds=(source.fd,),
                check=True,
            )
            assert completed.stdout == b"fd-payload"


def test_publish_anchors_to_the_verified_directory() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outside = root / "outside"
        outside.mkdir()
        outputs = root / "outputs"
        outputs.mkdir()
        slot = anchor_output_file(root, "outputs/published.txt")
        staging = create_staging(slot.directory, prefix="stage")
        staging.file_object().write(b"task output")
        # swap the output parent for a symlink out of the workspace after preflight
        outputs.rename(root / "original-outputs")
        outputs.symlink_to(outside, target_is_directory=True)
        outcome = publish_file(staging, slot, overwrite=False)
        assert outcome.published is True
        assert outcome.staging_removed is True
        assert (outside / "published.txt").exists() is False
        assert read_child(slot.directory, "published.txt") == b"task output"
        assert (root / "original-outputs" / "published.txt").read_bytes() == b"task output"


def test_publish_output_leaf_replacement_cannot_be_claimed_via_symlink() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "outputs").mkdir()
        outside = root / "outside.png"
        outside.write_bytes(b"x")
        slot = anchor_output_file(root, "outputs/target.png")
        assert slot.exists is False
        # attacker plants a symlink at the target name after preflight
        (root / "outputs" / "target.png").symlink_to(outside)
        staging = create_staging(slot.directory, prefix="stage")
        staging.file_object().write(b"real")
        with pytest.raises(FileAuthorityError) as caught:
            publish_file(staging, slot, overwrite=False)
        assert caught.value.code == "OUTPUT_EXISTS"
        assert outside.read_bytes() == b"x"


def test_publish_reports_cleanup_failure_honestly() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "outputs").mkdir()
        slot = anchor_output_file(root, "outputs/result.txt")
        staging = create_staging(slot.directory, prefix="stage")
        staging.file_object().write(b"complete output")
        real_unlink = os.unlink
        staging_name = staging.directory.fd, staging.name
        calls = 0

        def failing_unlink(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1 and Path(str(path)).name == staging_name[1]:
                raise PermissionError("injected post-publication cleanup failure")
            return real_unlink(path, *args, **kwargs)

        import file_authority.anchored as anchored

        original = anchored.os.unlink
        anchored.os.unlink = failing_unlink
        try:
            outcome = publish_file(staging, slot, overwrite=False)
        finally:
            anchored.os.unlink = original
        assert outcome.published is True
        assert outcome.staging_removed is False
        assert read_child(slot.directory, slot.name) == b"complete output"


def test_publish_without_overwrite_is_race_safe(workspace: Path) -> None:
    slot = anchor_output_file(workspace, "out/result.png")
    staging = create_staging(slot.directory, prefix="stage")
    staging.file_object().write(b"new")
    publish_file(staging, slot, overwrite=False)
    assert read_child(slot.directory, "result.png") == b"new"

    staging = create_staging(slot.directory, prefix="stage")
    staging.file_object().write(b"second")
    with pytest.raises(FileAuthorityError) as caught:
        publish_file(staging, slot, overwrite=False)
    assert caught.value.code == "OUTPUT_EXISTS"
    assert read_child(slot.directory, "result.png") == b"new"
    assert not [n for n in os.listdir(slot.directory.materialized_path()) if n.startswith(".stage")]


def test_publish_overwrite_replaces(workspace: Path) -> None:
    slot = anchor_output_file(workspace, "out/result.png")
    write_child_exclusive(slot.directory, "result.png", b"old")
    staging = create_staging(slot.directory, prefix="stage")
    staging.file_object().write(b"new")
    publish_file(staging, slot, overwrite=True)
    assert read_child(slot.directory, "result.png") == b"new"


def test_anchor_output_file_requires_real_parents(workspace: Path) -> None:
    for raw in ("missing-dir/result.png", "in/a.wav/result.png"):
        with pytest.raises(FileAuthorityError) as caught:
            anchor_output_file(workspace, raw)
        assert caught.value.code in {"OUTPUT_INVALID", "PATH_FORBIDDEN"}, raw
    outside = workspace.parent / "outside.png"
    outside.write_bytes(b"x")
    (workspace / "out" / "link.png").symlink_to(outside)
    with pytest.raises(FileAuthorityError) as caught:
        anchor_output_file(workspace, "out/link.png")
    assert caught.value.code == "PATH_FORBIDDEN"


def test_anchor_output_file_writability_is_opt_in(workspace: Path) -> None:
    locked = workspace / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        anchor_output_file(workspace, "locked/result.png")
        with pytest.raises(FileAuthorityError) as caught:
            anchor_output_file(workspace, "locked/result.png", require_writable=True)
        assert caught.value.code == "OUTPUT_INVALID"
    finally:
        os.chmod(locked, 0o755)


def test_anchor_output_directory_does_not_create_the_final_name(workspace: Path) -> None:
    existing = anchor_output_directory(workspace, "out")
    assert existing.existed is True
    assert existing.directory is not None

    fresh = anchor_output_directory(workspace, "out/render-1")
    assert fresh.existed is False
    assert fresh.directory is None
    assert fresh.name == "render-1"
    assert not (workspace / "out" / "render-1").exists()

    with pytest.raises(FileAuthorityError) as caught:
        anchor_output_directory(workspace, "missing/render-1")
    assert caught.value.code == "OUTPUT_INVALID"

    (workspace / "out" / "link").symlink_to(workspace / "in")
    with pytest.raises(FileAuthorityError) as caught:
        anchor_output_directory(workspace, "out/link")
    assert caught.value.code == "PATH_FORBIDDEN"


def test_move_into_and_remove_child(workspace: Path) -> None:
    source_dir = anchor_root(workspace / "out")
    (workspace / "out" / "target").mkdir()
    target_slot = anchor_output_directory(workspace, "out/target")
    target = target_slot.open()
    write_child_exclusive(source_dir, "segment.mp4", b"segment")
    move_into(source_dir, "segment.mp4", target, "segment.mp4", overwrite=False)
    assert read_child(target, "segment.mp4") == b"segment"
    assert source_dir.child_status("segment.mp4")[0] == "missing"
    assert remove_child(target, "segment.mp4") is True
    assert remove_child(target, "segment.mp4") is False


def test_state_helpers_round_trip_and_limits(workspace: Path) -> None:
    directory = anchor_root(workspace / "out")
    assert write_child_exclusive(directory, "build.json", b"{}", fsync=False) is True
    assert write_child_exclusive(directory, "build.json", b"{}", fsync=False) is False
    replace_child_atomically(directory, "build.json", b'{"a": 1}')
    assert read_child(directory, "build.json") == b'{"a": 1}'
    with pytest.raises(FileAuthorityError) as caught:
        read_child(directory, "build.json", max_bytes=3)
    assert caught.value.code == "LIMIT_EXCEEDED"


def test_default_error_builds_neutral_error() -> None:
    assert default_error("CODE", "message").code == "CODE"


def test_materialized_path_fails_safely_after_replacement(workspace: Path) -> None:
    directory = anchor_root(workspace / "out")
    assert Path(directory.materialized_path()).is_dir()
    (workspace / "out").rename(workspace / "out-moved")
    outside = workspace.parent / "outside-dir"
    outside.mkdir()
    (workspace / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileAuthorityError) as caught:
        directory.materialized_path()
    assert caught.value.code == "PATH_FORBIDDEN"


def test_materialized_path_cannot_recover_a_replaced_binding_from_proc(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with anchor_root(workspace / "out") as directory:
        moved = workspace / "out-moved"
        (workspace / "out").rename(moved)
        (workspace / "out").mkdir()
        proc = f"/proc/self/fd/{directory.fd}"
        original_exists, original_readlink = os.path.exists, os.readlink
        monkeypatch.setattr(
            os.path, "exists", lambda name: name == proc or original_exists(name),
        )
        monkeypatch.setattr(
            os, "readlink", lambda name: str(moved) if name == proc else original_readlink(name),
        )
        with pytest.raises(FileAuthorityError) as caught:
            directory.materialized_path()
        assert caught.value.code == "PATH_FORBIDDEN"


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "nested/file", ".", ".."])
def test_child_operations_refuse_paths_before_io(workspace: Path, name: str) -> None:
    with anchor_root(workspace) as directory:
        for operation in (
            lambda: read_child(directory, name),
            lambda: write_child_exclusive(directory, name, b"x"),
            lambda: replace_child_atomically(directory, name, b"x"),
            lambda: remove_child(directory, name),
            lambda: directory.child_status(name),
            lambda: create_staging(directory, prefix=name),
            lambda: move_into(directory, "a", directory, name, overwrite=True),
        ):
            with pytest.raises(FileAuthorityError) as caught:
                operation()
            assert caught.value.code == "PATH_FORBIDDEN"


def test_fifo_input_and_state_fail_without_waiting_for_a_writer(workspace: Path) -> None:
    os.mkfifo(workspace / "pipe")
    code = """
import sys
from pathlib import Path
from file_authority import FileAuthorityError, anchor_root, open_input_file, read_child
root = Path(sys.argv[1])
with anchor_root(root) as directory:
    actions = (lambda: open_input_file(directory, 'pipe'), lambda: read_child(directory, 'pipe'))
    for action in actions:
        try:
            action()
        except FileAuthorityError as failure:
            assert failure.code == 'PATH_FORBIDDEN'
        else:
            raise AssertionError('FIFO was admitted')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(workspace)],
        timeout=5, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_output_slots_own_handles_without_closing_borrowed_root(workspace: Path) -> None:
    with anchor_root(workspace) as root:
        file_slot = anchor_output_file(root, "result.txt")
        file_slot.directory.close()
        directory_slot = anchor_output_directory(root, "fresh")
        directory_slot.close()
        with open_input_file(root, "in/a.wav") as source:
            assert source.read_bytes() == b"audio-bytes"


def test_move_records_success_when_source_cleanup_fails(workspace: Path, monkeypatch) -> None:
    (workspace / "in" / "item").write_bytes(b"keep")
    with anchor_root(workspace / "in") as source, anchor_root(workspace / "out") as target:
        unlink = os.unlink

        def fail_source(name, *, dir_fd=None):
            if name == "item" and dir_fd == source.fd:
                raise PermissionError("injected cleanup failure")
            return unlink(name, dir_fd=dir_fd)

        monkeypatch.setattr(os, "unlink", fail_source)
        outcome = move_into(source, "item", target, "item", overwrite=False)
        assert outcome.published and not outcome.staging_removed
        assert read_child(target, "item") == read_child(source, "item") == b"keep"
        with pytest.raises(FileAuthorityError) as caught:
            move_into(source, "item", target, "item", overwrite=False)
        assert caught.value.code == "OUTPUT_EXISTS"


def test_exclusive_directory_publication_preserves_racing_claim(workspace: Path) -> None:
    (workspace / "in" / "public").mkdir()
    (workspace / "in" / "public" / "item").write_bytes(b"rendered")
    with anchor_root(workspace / "in") as source, anchor_root(workspace / "out") as target:
        raced = workspace / "out" / "final"
        raced.mkdir()
        inode = raced.stat().st_ino
        with pytest.raises(FileAuthorityError) as caught:
            publish_directory(source, "public", target, "final")
        assert caught.value.code == "OUTPUT_EXISTS"
        assert raced.stat().st_ino == inode
        assert list(raced.iterdir()) == []
        raced.rmdir()
        publish_directory(source, "public", target, "final")
        assert (raced / "item").read_bytes() == b"rendered"


def test_copy_checks_cancellation_and_removes_partial_output(workspace: Path) -> None:
    def cancelled():
        raise RuntimeError("cancelled")

    with open_input_file(workspace, "in/a.wav") as source, anchor_root(workspace / "out") as target:
        with pytest.raises(RuntimeError, match="cancelled"):
            source.copy_into(target, "item", check=cancelled)
        assert target.child_status("item")[0] == "missing"
