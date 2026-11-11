"""Immutable raw archive.

Only this module writes raw/<item_id>/<analysis_hash>.md. The file stores the
title, plain body, channel, prompt audience, and transcript source markers.
Interaction counts are omitted. A repeat call with the same bytes does not
rewrite the file.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from second_brain.errors import IdentityCollision, SchemaError, StorageError
from second_brain.models import IngestRecord, canonical_json, prompt_fields


class RawArchive:
    """Append-only raw documents. put returns a path relative to raw_dir and its SHA256."""

    def __init__(self, raw_dir: Path) -> None:
        if not isinstance(raw_dir, Path):
            raise StorageError("invalid_path")
        self.raw_dir = raw_dir

    def put(self, record: IngestRecord) -> tuple[str, str]:
        """Write one version if absent. Identical bytes are kept, including mtime."""
        if not isinstance(record, IngestRecord):
            raise SchemaError("invalid_record")
        root = _root(self.raw_dir)
        item_id = record.item.id
        analysis = record.item.analysis_hash
        if _unsafe_name(item_id) or _unsafe_name(analysis):
            raise StorageError("invalid_raw_path")
        relative = f"{item_id}/{analysis}.md"
        parent = root / item_id
        dest = parent / f"{analysis}.md"
        payload = _document(record)
        digest = hashlib.sha256(payload).hexdigest()
        _reject_symlink(parent)
        _reject_symlink(dest)
        _contained(root, parent)
        _contained(root, dest)
        if dest.exists():
            if _read_nofollow(dest) == payload:
                return relative, digest
            raise IdentityCollision()
        if not parent.exists():
            parent.mkdir()
        if parent.is_symlink() or not parent.is_dir():
            raise StorageError("symlink_escape")
        _contained(root, parent)
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=parent)
            tmp_path = Path(tmp_name)
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp_path, dest)
            except FileExistsError:
                if _read_nofollow(dest) == payload:
                    return relative, digest
                raise IdentityCollision()
            _fsync_dir(parent)
        except IdentityCollision:
            raise
        except StorageError:
            raise
        except OSError:
            raise StorageError("raw_io") from None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return relative, digest


def _document(record: IngestRecord) -> bytes:
    fields = prompt_fields(record.item, record.asset)
    payload = {
        "audience": fields["audience"],
        "body": record.item.body_plain,
        "channel": record.asset.channel,
        "title": record.item.title,
        "transcript_status": record.asset.transcript_status,
        "transcript_type": record.asset.transcript_type,
        "transcript_url": record.asset.transcript_url,
    }
    return ("# raw\n" + canonical_json(payload) + "\n").encode("utf-8")


def _unsafe_name(name: str) -> bool:
    return name in {"", ".", ".."} or "/" in name or "\\" in name


def _root(raw_dir: Path) -> Path:
    root = Path(raw_dir)
    if root.is_symlink():
        raise StorageError("symlink_escape")
    if not root.exists():
        parent = root.parent
        if parent.is_symlink() or not parent.is_dir():
            raise StorageError("not_a_directory")
        root.mkdir()
    if root.is_symlink() or not root.is_dir():
        raise StorageError("not_a_directory")
    return root


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise StorageError("symlink_escape")


def _contained(root: Path, path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    root_real = root.resolve(strict=True)
    real = path.resolve(strict=True)
    if not real.is_relative_to(root_real):
        raise StorageError("symlink_escape")


def _read_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        if path.is_symlink():
            raise StorageError("symlink_escape") from None
        raise StorageError("raw_io") from None
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise StorageError("raw_io")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
