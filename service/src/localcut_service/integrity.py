"""Process-local memoization for read-only integrity checks in polling APIs."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import Callable

from .persistence import _sha256_file


class IntegrityDigestCache:
    """Reuse a verified digest while every file's size and mtime remain stable.

    Callers that require a fresh content hash can pass ``force=True``. Job
    validation deliberately continues to hash inputs directly and does not use
    this cache.
    """

    def __init__(self, hasher: Callable[[Path], str] = _sha256_file, max_entries: int = 256):
        self._hasher = hasher
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, tuple[tuple[tuple[str, str, int, int], ...], str]] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _absolute_path(path: Path) -> Path:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise ValueError("Artifact symlinks are not allowed.")
        return absolute

    @staticmethod
    def _signature(path: Path) -> tuple[tuple[str, str, int, int], ...]:
        if path.is_symlink():
            raise ValueError("Artifact symlinks are not allowed.")
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            stat = path.stat()
            return ((".", "file", stat.st_size, stat.st_mtime_ns),)
        if not path.is_dir():
            raise ValueError("Integrity checks require a regular file or directory.")

        rows: list[tuple[str, str, int, int]] = []
        for item in sorted(path.rglob("*"), key=lambda candidate: candidate.relative_to(path).as_posix()):
            if item.is_symlink():
                raise ValueError("Artifact symlinks are not allowed.")
            stat = item.stat()
            if item.is_dir():
                kind = "directory"
            elif item.is_file():
                kind = "file"
            else:
                raise ValueError("Artifact contains a non-regular file.")
            rows.append((item.relative_to(path).as_posix(), kind, stat.st_size, stat.st_mtime_ns))
        if not rows:
            raise ValueError("Artifact bundle is empty.")
        return tuple(rows)

    def digest(self, path: Path, *, force: bool = False) -> tuple[str, bool]:
        """Return ``(sha256, cache_hit)`` for a file or artifact directory."""
        absolute = self._absolute_path(path)
        key = str(absolute)
        signature = self._signature(absolute)
        if not force:
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None and cached[0] == signature:
                    self._entries.move_to_end(key)
                    return cached[1], True

        digest = self._hasher(absolute)
        after = self._signature(absolute)
        with self._lock:
            if signature == after:
                self._entries[key] = (after, digest)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            else:
                self._entries.pop(key, None)
        return digest, False
