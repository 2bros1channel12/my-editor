from __future__ import annotations

import hashlib

import pytest

from localcut_service.integrity import IntegrityDigestCache


def test_file_digest_reuses_unchanged_metadata_and_force_rechecks(tmp_path):
    path = tmp_path / "result.json"
    path.write_bytes(b"first payload")
    calls = []

    def hasher(target):
        calls.append(target)
        return hashlib.sha256(target.read_bytes()).hexdigest()

    cache = IntegrityDigestCache(hasher)
    first, first_hit = cache.digest(path)
    second, second_hit = cache.digest(path)
    forced, forced_hit = cache.digest(path, force=True)

    assert first == second == forced
    assert first_hit is False
    assert second_hit is True
    assert forced_hit is False
    assert len(calls) == 2

    path.write_bytes(b"replacement payload")
    changed, changed_hit = cache.digest(path)
    assert changed == hashlib.sha256(b"replacement payload").hexdigest()
    assert changed != first
    assert changed_hit is False
    assert len(calls) == 3


def test_directory_digest_cache_tracks_child_file_changes(tmp_path):
    bundle = tmp_path / "proxy-cache"
    bundle.mkdir()
    media = bundle / "proxy.mp4"
    media.write_bytes(b"proxy-a")
    calls = []

    def hasher(target):
        calls.append(target)
        digest = hashlib.sha256()
        for child in sorted(target.rglob("*")):
            if child.is_file():
                digest.update(child.relative_to(target).as_posix().encode())
                digest.update(child.read_bytes())
        return digest.hexdigest()

    cache = IntegrityDigestCache(hasher)
    original, miss = cache.digest(bundle)
    assert cache.digest(bundle) == (original, True)
    assert miss is False

    media.write_bytes(b"proxy-b-has-a-different-size")
    updated, hit = cache.digest(bundle)
    assert updated != original
    assert hit is False
    assert len(calls) == 2


def test_digest_cache_rejects_symlinked_artifact_root(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("artifact", encoding="utf-8")
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable on this host: {exc}")

    cache = IntegrityDigestCache()
    with pytest.raises(ValueError, match="symlinks are not allowed"):
        cache.digest(alias)
