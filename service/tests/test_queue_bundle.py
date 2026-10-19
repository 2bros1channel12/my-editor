from pathlib import Path
import os

from localcut_service.jobs import JobInput, JobOutput, JobQueue
from localcut_service.persistence import ProjectStore, _connect, _sha256_file


class BundleHandler:
    kind = "proxy"
    retry_safe = True

    def validate_input(self, payload):
        return payload

    def run(self, context, payload):
        root = context.staging_file(".bundle")
        root.mkdir()
        (root / "proxy.mp4").write_bytes(b"video")
        (root / "map.json").write_text('{"mapping": []}')
        assert context.input.generator_id == "ffmpeg"
        assert context.input.config == {"size": 720}
        return JobOutput(str(root), payload.get("target", "cache/proxies/preview"), "proxy")

    def validate_output(self, output):
        assert Path(output.staging_path).is_dir()


def job(store, **payload):
    return JobInput(str(store.root), "proxy", payload, "source", generator_id="ffmpeg", generator_version="test", config={"size": 720})


def test_bundle_checksum_dedup_and_provenance(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Test")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": BundleHandler()})
    try:
        result = queue.wait(queue.submit(job(store))["id"])
        assert result["state"] == "completed"
        root = store.root / result["output"]["relativePath"]
        assert root.is_dir()
        assert queue.submit(job(store))["deduplicated"]
        with _connect(store.database_path) as db:
            artifact = db.execute("SELECT * FROM artifacts").fetchone()
            assert artifact["generator_id"] == "ffmpeg"
            assert artifact["generator_version"] == "test"
        (root / "proxy.mp4").write_bytes(b"corrupt")
        assert store.find_valid_artifact(queue._get(result["id"])["dedupe_key"]) is None
    finally:
        queue.close()


def test_preexisting_target_is_never_deleted(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Test")
    target = store.root / "cache/proxies/preview"
    target.mkdir(parents=True)
    (target / "mine.txt").write_text("preserve")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": BundleHandler()})
    try:
        assert queue.wait(queue.submit(job(store))["id"])["state"] == "failed"
        assert (target / "mine.txt").read_text() == "preserve"
    finally:
        queue.close()


def test_cancel_after_publish_cleans_owned_bundle(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Test")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": BundleHandler()}, autostart=False)
    submitted = queue.submit(job(store))
    original = os.rename
    def rename_and_cancel(source, target):
        original(source, target)
        queue.cancel(submitted["id"])
    monkeypatch.setattr(os, "rename", rename_and_cancel)
    try:
        queue.start()
        assert queue.wait(submitted["id"])["state"] == "cancelled"
        assert not (store.root / "cache/proxies/preview").exists()
    finally:
        queue.close()


def test_recovery_removes_published_uncommitted_bundle(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Test")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": BundleHandler()}, autostart=False)
    submitted = queue.submit(job(store))
    row = queue._claim()
    staged = queue._project_stage(row) / "bundle"
    staged.mkdir(parents=True)
    (staged / "proxy.mp4").write_bytes(b"partial publication")
    stat = staged.stat()
    target = store.root / "cache/proxies/preview"
    target.parent.mkdir(parents=True, exist_ok=True)
    with _connect(queue.database_path) as db:
        db.execute("INSERT INTO publications VALUES (?,?,?,?,?)", (submitted["id"], str(target), _sha256_file(staged), stat.st_dev, stat.st_ino))
        db.commit()
    os.rename(staged, target)
    queue.close()
    reopened = JobQueue(tmp_path / "app", handlers={"proxy": BundleHandler()}, autostart=False)
    try:
        assert not target.exists()
        assert reopened._get(submitted["id"])["state"] == "retrying"
        reopened.start()
        assert reopened.wait(submitted["id"])["state"] == "completed"
    finally:
        reopened.close()
