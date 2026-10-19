from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from localcut_service.jobs import JobContextImpl, JobInput, JobOutput, JobQueue, TransientJobError
from localcut_service.persistence import ProjectStore


class FileHandler:
    kind = "proxy"
    retry_safe = True

    def validate_input(self, payload):
        if "content" not in payload:
            raise ValueError("content required")
        return payload

    def run(self, context, payload):
        context.report(0.4, "writing", "Generating test artifact")
        path = context.staging_file(".bin")
        path.write_bytes(payload["content"].encode())
        return JobOutput(str(path), f"cache/proxies/{context.job_id}.bin", "proxy", {"fixture": True})

    def validate_output(self, output):
        assert Path(output.staging_path).is_file()


def _job(store: ProjectStore, content: str = "valid") -> JobInput:
    return JobInput(str(store.root), "proxy", {"content": content}, "source-fingerprint", max_attempts=3)


def test_job_output_is_validated_committed_then_completed(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": FileHandler()}, concurrency=1)
    try:
        submitted = queue.submit(_job(store))
        job = queue.wait(submitted["id"])
        assert job["state"] == "completed"
        assert job["progress"] == 1
        output = job["output"]
        final = store.root / output["relativePath"]
        assert final.read_bytes() == b"valid"
        assert not (queue.stage_root / submitted["id"]).exists()
        assert queue.list_jobs(store.project_id)[0]["state"] == "completed"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and len(store.recent_job_events(submitted["id"])) < 4:
            time.sleep(0.01)
        assert [event["event"] for event in store.recent_job_events(submitted["id"])] == ["queued", "started", "progress", "completed"]
    finally:
        queue.close()


def test_invalid_or_empty_output_never_completes_and_staging_is_removed(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    class EmptyHandler(FileHandler):
        def run(self, context, payload):
            path = context.staging_file(".bin")
            path.write_bytes(b"")
            return JobOutput(str(path), f"cache/proxies/{context.job_id}.bin", "proxy")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": EmptyHandler()}, concurrency=1)
    try:
        submitted = queue.submit(_job(store))
        job = queue.wait(submitted["id"])
        assert job["state"] == "failed"
        assert job["output"] is None
        assert not (store.root / f"cache/proxies/{submitted['id']}.bin").exists()
        assert not (queue.stage_root / submitted["id"]).exists()
    finally:
        queue.close()


def test_queued_cancellation_and_running_cooperative_cancellation(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": FileHandler()}, autostart=False)
    queued = queue.submit(_job(store))
    assert queue.cancel(queued["id"])["state"] == "cancelled"

    entered = threading.Event()
    class SlowHandler(FileHandler):
        def run(self, context, payload):
            entered.set()
            while True:
                time.sleep(0.01)
                context.check_cancelled()
    queue.register_handler("proxy", SlowHandler())
    running = queue.submit(_job(store, "cancel me"))
    queue.start()
    try:
        assert entered.wait(2)
        queue.cancel(running["id"])
        assert queue.wait(running["id"])["state"] == "cancelled"
        assert not (queue.stage_root / running["id"]).exists()
    finally:
        queue.close()


def test_hot_loop_cancellation_checks_do_not_query_sqlite_for_every_sample(tmp_path: Path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    queue = JobQueue(tmp_path / "app", autostart=False)
    calls = 0
    original = queue._cancel_requested
    def count_polls(job_id):
        nonlocal calls
        calls += 1
        return original(job_id)
    monkeypatch.setattr(queue, "_cancel_requested", count_polls)
    context = JobContextImpl(queue, "not-a-real-job", _job(store), queue.stage_root / "test")
    try:
        for _ in range(20_000):
            context.check_cancelled()
        assert calls < 5
    finally:
        queue.close()


def test_transient_retry_then_success_and_permanent_failure(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    attempts = {"n": 0}
    class RetryHandler(FileHandler):
        def run(self, context, payload):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TransientJobError("temporary")
            return super().run(context, payload)
    queue = JobQueue(tmp_path / "app", handlers={"proxy": RetryHandler()}, concurrency=1)
    try:
        first = queue.submit(_job(store))
        assert queue.wait(first["id"], timeout=4)["state"] == "completed"
        assert attempts["n"] == 2

        class BadHandler(FileHandler):
            def run(self, context, payload):
                raise ValueError("permanent")
        queue.register_handler("proxy", BadHandler())
        second = queue.submit(_job(store, "bad"))
        result = queue.wait(second["id"])
        assert result["state"] == "failed"
        assert result["attempts"] == 1
    finally:
        queue.close()


def test_duplicate_job_reuses_only_valid_committed_artifact(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": FileHandler()}, concurrency=1)
    try:
        first = queue.submit(_job(store))
        assert queue.wait(first["id"])["state"] == "completed"
        duplicate = queue.submit(_job(store))
        assert duplicate["state"] == "completed"
        assert duplicate["deduplicated"] is True
        assert duplicate["artifactId"] == queue.list_jobs(store.project_id)[0]["output"]["artifactId"]
    finally:
        queue.close()


def test_restart_recovers_interrupted_retry_safe_job_and_cleans_only_its_stage(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    app_data = tmp_path / "app"
    queue = JobQueue(app_data, handlers={"proxy": FileHandler()}, autostart=False)
    first = queue.submit(_job(store))
    from localcut_service.persistence import _connect
    with _connect(queue.database_path) as db:
        db.execute("UPDATE queue_jobs SET state='running',stage='encoding',attempts=1 WHERE id=?", (first["id"],))
        db.commit()
    partial = queue.stage_root / first["id"] / "output.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"incomplete")
    unrelated = queue.stage_root / "unrelated" / "keep.part"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"keep")
    queue.close()

    recovered = JobQueue(app_data, handlers={"proxy": FileHandler()}, autostart=False)
    try:
        job = recovered.list_jobs(store.project_id)[0]
        assert job["state"] == "retrying"
        assert not partial.exists()
        assert unrelated.read_bytes() == b"keep"
        recovered.start()
        assert recovered.wait(first["id"])["state"] == "completed"
    finally:
        recovered.close()


def test_dependency_waits_for_parent_and_invalid_input_is_rejected(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    queue = JobQueue(tmp_path / "app", handlers={"proxy": FileHandler()}, autostart=False)
    try:
        with pytest.raises(ValueError, match="content required"):
            queue.submit(JobInput(str(store.root), "proxy", {}, "x"))
        parent = queue.submit(_job(store, "parent"))
        child = queue.submit(JobInput(str(store.root), "proxy", {"content": "child"}, "child-source", dependencies=(parent["id"],)))
        assert queue._get(child["id"])["state"] == "queued"
        queue.start()
        assert queue.wait(parent["id"])["state"] == "completed"
        assert queue.wait(child["id"])["state"] == "completed"
    finally:
        queue.close()


def test_only_one_service_process_owns_the_persistent_queue(tmp_path: Path):
    queue = JobQueue(tmp_path / "app", autostart=False)
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            JobQueue(tmp_path / "app", autostart=False)
    finally:
        queue.close()
    next_owner = JobQueue(tmp_path / "app", autostart=False)
    next_owner.close()
