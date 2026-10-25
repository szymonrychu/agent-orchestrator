"""Pack a session workdir into a capped tar.zst stream and unpack one safely (design D5, D6).

Save never follows symlinks and skips sockets, FIFOs and device files. Restore streams through
`tarfile`'s `data` filter (no absolute paths, no `..`, no links out of the workdir, no device
nodes, setuid bits cleared), refuses sparse members and anything past a decompressed byte budget,
and never deletes or empties what is already in the workdir.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import tarfile
from pathlib import Path
from typing import BinaryIO

import zstandard

from agents_orchestrator.spec import CACHE_ALWAYS_EXCLUDE

# The single-PUT limit of S3 and RGW, and so the largest compressed archive (gate Q2).
MAX_ARCHIVE_BYTES = 5 * 1024**3
# Decompressed budget of one restore: a decompression bomb stops here, not at a full volume.
UNPACK_MAX_BYTES = 10 * MAX_ARCHIVE_BYTES
COMPRESSION_LEVEL = 3


class TooLarge(Exception):
    """The compressed archive passed its cap; the caller keeps the previous archive."""


class ExtractError(Exception):
    """A corrupt stream, a refused member, or a restore past its decompressed budget."""


def _require_workdir(workdir: Path) -> None:
    """Refuse an empty or root workdir before anything touches the filesystem."""
    text = os.fspath(workdir)
    if not text or os.path.normpath(text) == "." or not os.path.abspath(text).strip("/"):
        raise ValueError(f"refusing workdir {text!r}: empty or the filesystem root")


class _CappedWriter:
    """Forwards compressed output to `out`, raising TooLarge before it would pass the cap."""

    def __init__(self, out: BinaryIO, max_bytes: int) -> None:
        self._out = out
        self._max = max_bytes
        self.written = 0

    def write(self, data: bytes) -> int:
        size = len(data)
        if self.written + size > self._max:
            raise TooLarge(f"compressed archive passes {self._max} bytes")
        self._out.write(data)
        self.written += size
        return size

    def flush(self) -> None:
        self._out.flush()


def _excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)


def _add(tar: tarfile.TarFile, path: str, rel: str) -> None:
    """Add one entry by lstat: directories, regular files and symlinks; nothing else."""
    try:
        mode = os.lstat(path).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            return
        info = tar.gettarinfo(path, arcname=rel)
        if info.isreg():
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
        else:
            tar.addfile(info)
    except FileNotFoundError:
        return  # removed while the workdir was walked


def pack(workdir: Path, out: BinaryIO, *, exclude: list[str], max_bytes: int) -> int:
    """Write `workdir` as a zstd-compressed tar to `out`; return the compressed bytes written.

    Members are workdir-relative POSIX paths. `.agent-init` and each `exclude` glob (fnmatch on
    the relative path) are left out; an excluded directory prunes its subtree. Raises TooLarge
    as soon as the compressed output would pass `max_bytes`.
    """
    _require_workdir(workdir)
    root = os.fspath(workdir)
    patterns = [*CACHE_ALWAYS_EXCLUDE, *exclude]
    capped = _CappedWriter(out, max_bytes)
    compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)
    with (
        compressor.stream_writer(capped, closefd=False) as zst,
        tarfile.open(fileobj=zst, mode="w|", format=tarfile.PAX_FORMAT) as tar,
    ):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = os.path.relpath(dirpath, root)
            kept = []
            for name in sorted(dirnames):
                rel = name if base == "." else f"{base}/{name}"
                if not _excluded(rel, patterns):
                    kept.append(name)
                    _add(tar, os.path.join(dirpath, name), rel)
            dirnames[:] = kept  # prunes excluded subtrees; symlinked dirs are never entered
            for name in sorted(filenames):
                rel = name if base == "." else f"{base}/{name}"
                if not _excluded(rel, patterns):
                    _add(tar, os.path.join(dirpath, name), rel)
    return capped.written


class _BudgetReader:
    """Reads the decompressed tar stream, refusing it once it passes `max_bytes`."""

    def __init__(self, src: zstandard.ZstdDecompressionReader, max_bytes: int) -> None:
        self._src = src
        self._max = max_bytes
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        data = self._src.read(size)
        self.read_bytes += len(data)
        if self.read_bytes > self._max:
            raise ExtractError(f"archive expands past {self._max} bytes")
        return data


def unpack(src: BinaryIO, workdir: Path, *, max_bytes: int = UNPACK_MAX_BYTES) -> int:
    """Extract a zstd-compressed tar from `src` into `workdir`; return the members extracted.

    `workdir` is created when missing and nothing already in it is deleted. A corrupt stream, a
    refused member (absolute path, `..`, a link escaping `workdir`, a device node or FIFO, a
    sparse file) or more than `max_bytes` of decompressed data raises ExtractError. Errors
    reading `src` itself propagate unchanged, so a failed download is never an extract failure.
    """
    _require_workdir(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    reader = _BudgetReader(
        zstandard.ZstdDecompressor().stream_reader(src, closefd=False), max_bytes
    )
    count = 0
    declared = 0
    try:
        with tarfile.open(fileobj=reader, mode="r|", errorlevel=2) as tar:
            for member in tar:
                if member.name.startswith("/"):
                    # The data filter would strip the slash; an absolute member is refused.
                    raise ExtractError(f"refusing absolute member {member.name!r}")
                if member.issparse():
                    raise ExtractError(f"refusing sparse member {member.name!r}")
                declared += member.size
                if declared > max_bytes:
                    raise ExtractError(f"archive expands past {max_bytes} bytes")
                tar.extract(member, workdir, filter="data")
                count += 1
    except (tarfile.TarError, zstandard.ZstdError) as exc:
        raise ExtractError(str(exc)) from exc
    return count
