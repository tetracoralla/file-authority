# File Authority

`openadam-file-authority` is a dependency-free Python library that owns the
workspace file rules the openAdam Agent tools share: bounded
workspace-relative path validation, symlink-rejecting resolution anchored to
open descriptors, output preflight, and atomic single-file publication. Asset
Prep, Rhythm Tool, and Timeline Video Engine consume it and keep their own
error types, codes, human-CLI paths, multi-file transactions, and recovery
semantics.

## Checks and use refer to the same object

Earlier path-based "check now, reopen by path later" resolvers could be
fooled by renaming or symlink-planting a path component between the check and
the actual read or write, letting I/O escape the granted workspace. This
library instead anchors the granted object before handing it out:

- `anchor_root(root)` opens the granted workspace root (resolved strictly,
  since the root is the host's grant) as a directory descriptor.
- `open_input_file(root, raw, *, max_bytes)` walks every component with
  `openat` + `O_NOFOLLOW` and returns an `OpenInput` holding the verified
  file descriptor. Reads always target the opened inode: replacing the leaf
  or any ancestor afterwards cannot redirect what the caller
  consumes. `size` is the snapshot at open time; library reads and copies
  are bounded by it, so in-place growth does not increase their byte budget.
  (Content rewritten in place through the same inode remains
  visible — the guarantee is identity and budget, not immutability.)
- `anchor_output_file(root, raw, ...)` / `anchor_output_directory(root, raw)`
  return slots whose parent directory is held open; publishing operates on
  names inside that verified directory.
- `create_staging`, `publish_file`, and `move_into` publish with
  `linkat`/`renameat` through directory descriptors, so a swapped output
  ancestor cannot place a file outside the workspace. A missing final output
  directory name is deliberately not created by anchoring; publication can
  place a rendered directory into the name with `publish_directory`, an
  exclusive rename on macOS/Linux that refuses a racing output claim.
- `AnchoredDirectory.materialized_path()` is the verified escape hatch for
  external programs that can only take a path: the current binding is
  re-verified by inode and the call fails safely (`PATH_FORBIDDEN`) if the
  directory was renamed or replaced. `OpenInput.subprocess_reference()`
  returns `/dev/fd/<fd>` (or `/proc/self/fd/<fd>`) for direct subprocess
  input; the caller must pass the descriptor through (`pass_fds`) and there
  is no silent fallback. Raw descriptors used by external programs preserve
  identity but do not enforce the snapshot read limit; the consumer owns
  that process's limits. A materialized directory path is verified only at
  the instant it is returned: external writes need caller-owned private
  staging followed by anchored publication.
- `read_child` / `write_child_exclusive` / `replace_child_atomically` give
  product-owned state directories the same anchored guarantees.

## Honest publication semantics

`publish_file` returns `PublishOutcome(published, staging_removed)`. Without
`overwrite`, the output name is claimed by hard-link creation, which is
atomic against concurrent writers; a racing claim fails with
`OUTPUT_EXISTS` and nothing was published. If the staging file cannot be
removed *after* a successful link, the outcome reports
`published=True, staging_removed=False` instead of misreporting a completed
publication as a failure. Consumers surface that state honestly in their
own contracts.

`move_into` returns the same outcome: once a no-overwrite link succeeds,
source cleanup failure is a published result, so a product transaction can
record and roll back the target correctly. Direct-child helpers accept exactly
one relative name. Output slots own independent directory handles; closing a
slot does not close the root supplied by its caller. File opens are nonblocking
until the regular-file check, so FIFOs fail without waiting for a writer.

## Error boundary

Checks fail by raising the exception built by `error` — `FileAuthorityError`
by default. Products pass a factory returning their own error type so their
public codes (`PATH_FORBIDDEN`, `SOURCE_NOT_FOUND`, `OUTPUT_INVALID`,
`OUTPUT_EXISTS`, `LIMIT_EXCEEDED`, `INVALID_INPUT`) and exception types are
unchanged. Scheme-prefixed names (`notes:v1.png`) are refused everywhere.

## Guarantees and limits

- Renaming or symlink-planting input leaves/ancestors, output
  leaves/ancestors, or the output directory between anchoring and use
  cannot make the library read or write outside the granted workspace: the
  anchored object keeps being used, or the operation fails safely.
- `publish_file` is single-file and same-directory-filesystem. Multi-file
  transactions and their rollback stay with the owning product (Timeline
  builds its publication on `move_into` / `AnchoredDirectory` primitives).
- No locks are taken; the no-overwrite link protocol makes a single writer
  deterministic without an inter-process lock.
- Adversarial control from the *same user* over files the user can already
  write inside a workspace (for example swapping the contents of a staging
  file the product itself created) is not the boundary this library claims.
- No workspace grants, content quarantine, or permission prompts: the owning
  tool and host keep those responsibilities.

## Install

Install the versioned [GitHub release artifact](https://github.com/tetracoralla/file-authority/releases/tag/v0.2.1) directly:

```sh
python -m pip install https://github.com/tetracoralla/file-authority/releases/download/v0.2.1/openadam_file_authority-0.2.1-py3-none-any.whl
```

Once this version is available on PyPI, registry installation is:

```sh
python -m pip install openadam-file-authority==0.2.1
```

```python
from pathlib import Path
from file_authority import anchor_root, open_input_file

# The application chooses and grants the workspace root.
with anchor_root(Path("./workspace")) as root:
    with open_input_file(root, "input.txt", max_bytes=1_000_000) as source:
        data = source.read_bytes()
```

Apache-2.0, Python >= 3.11, zero runtime dependencies. Supported systems are
macOS and Linux: the implementation uses descriptor-relative filesystem APIs,
`O_NOFOLLOW`, and platform-specific exclusive directory rename. It is not a
Windows filesystem authority implementation. CI exercises the minimum Python
version and a current Python version on both supported systems. Importing an
installed wheel requires no sibling checkout. Offline builds may vendor the
verified release wheel; lockfiles retain its digest. See [RELEASING.md](RELEASING.md).

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python scripts/check-package.py
uv build
```
