"""Explicit, reviewable deletion of rebuildable project preview/analysis caches."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .persistence import ProjectError, ProjectStore, _connect


def _safe_artifact_path(store: ProjectStore, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "cache":
        raise ProjectError("A cache artifact has an unsafe project-relative path; no files were removed.")
    if len(relative.parts) < 3 or relative.parts[1] not in {"proxies", "analysis"}:
        raise ProjectError("A cache artifact is outside the supported cleanup scope; no files were removed.")
    target = store.root / relative
    current = store.root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ProjectError("A cache path contains a symbolic link; no files were removed.")
    resolved = target.resolve(strict=False)
    cache_root = (store.root / "cache").resolve()
    try:
        resolved.relative_to(cache_root)
    except ValueError as exc:
        raise ProjectError("A cache artifact resolves outside the project cache; no files were removed.") from exc
    return resolved


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink():
        raise ProjectError("A cache path contains a symbolic link; no files were removed.")
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.iterdir():
        if child.is_symlink():
            raise ProjectError("A cache path contains a symbolic link; no files were removed.")
        if child.is_dir():
            total += _tree_size(child)
        elif child.is_file():
            total += child.stat().st_size
    return total


def cache_snapshot(store: ProjectStore) -> dict[str, Any]:
    with _connect(store.database_path) as db:
        active = [dict(row) for row in db.execute(
            "SELECT id,kind,state FROM jobs WHERE project_id=? AND state IN ('queued','running','retrying') ORDER BY created_at",
            (store.project_id,),
        )]
        rows = db.execute(
            "SELECT id,kind,relative_path,status,checksum_sha256 FROM artifacts "
            "WHERE project_id=? AND (kind='proxy' OR kind LIKE 'analysis:%') ORDER BY relative_path,id",
            (store.project_id,),
        ).fetchall()

    entries = []
    fingerprint_rows = []
    seen_paths: set[str] = set()
    for row in rows:
        path = _safe_artifact_path(store, row["relative_path"])
        canonical = str(path)
        if canonical in seen_paths:
            raise ProjectError("Multiple cache records point to the same path; no files were removed.")
        seen_paths.add(canonical)
        size = _tree_size(path)
        stat = path.stat() if path.exists() else None
        fingerprint_rows.append({
            "id": row["id"], "kind": row["kind"], "relativePath": row["relative_path"],
            "status": row["status"], "checksum": row["checksum_sha256"], "size": size,
            "mtimeNs": stat.st_mtime_ns if stat else None,
        })
        if path.exists():
            entries.append({
                "artifactId": row["id"], "kind": row["kind"], "relativePath": row["relative_path"],
                "path": str(path), "status": row["status"], "sizeBytes": size,
            })

    token_source = json.dumps({
        "projectId": store.project_id, "entries": fingerprint_rows,
        "activeJobs": active,
    }, sort_keys=True, separators=(",", ":"))
    token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()
    return {
        "projectId": store.project_id,
        "projectPath": str(store.root),
        "entries": entries,
        "artifactCount": len(entries),
        "totalBytes": sum(item["sizeBytes"] for item in entries),
        "activeJobs": active,
        "canClear": not active and bool(entries),
        "confirmationToken": token if not active and entries else None,
        "protectedPaths": ["media/", "exports/", "models/", "transcripts in SQLite", "project database and history", "recovery/"],
    }


def clear_generated_cache(store: ProjectStore, confirmation_token: str) -> dict[str, Any]:
    if not isinstance(confirmation_token, str) or len(confirmation_token) != 64:
        raise ProjectError("Review the current cache list before clearing it.")
    snapshot = cache_snapshot(store)
    if snapshot["activeJobs"]:
        raise ProjectError("Wait for this project's queued or running jobs to finish before clearing preview caches.")
    if not snapshot["entries"]:
        return {"clearedCount": 0, "clearedBytes": 0, "snapshot": snapshot}
    if snapshot["confirmationToken"] != confirmation_token:
        raise ProjectError("The project cache changed after review. Refresh the list and confirm again.")

    artifact_ids = [item["artifactId"] for item in snapshot["entries"]]
    with _connect(store.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        # Invalidate indexes before filesystem work. A crash can leave an orphan
        # cache file, but it can never be reused as a ready artifact.
        placeholders = ",".join("?" for _ in artifact_ids)
        db.execute(f"UPDATE artifacts SET status='stale' WHERE project_id=? AND id IN ({placeholders})", (store.project_id, *artifact_ids))
        db.execute(f"UPDATE analyses SET status='stale' WHERE project_id=? AND artifact_id IN ({placeholders})", (store.project_id, *artifact_ids))
        db.commit()

    deleted_count = 0
    deleted_bytes = 0
    failures = []
    for entry in snapshot["entries"]:
        try:
            target = _safe_artifact_path(store, entry["relativePath"])
            size = _tree_size(target)
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            deleted_count += 1
            deleted_bytes += size
        except (OSError, ProjectError) as exc:
            failures.append({"path": entry["path"], "error": str(exc)})
    return {
        "clearedCount": deleted_count,
        "clearedBytes": deleted_bytes,
        "failures": failures,
        "snapshot": cache_snapshot(store),
    }
