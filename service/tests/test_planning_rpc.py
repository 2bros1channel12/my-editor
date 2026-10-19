from localcut_service.config import AppConfig
from localcut_service.server import dispatch
from localcut_service.persistence import ProjectStore
import pytest


def test_written_plans_are_isolated_from_executable_proposals(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Review")
    written = store.add_edit_plan({"planType": "written-rough-cut"}, status="pending", rationale=[], generator_id="fixture", generator_version="1")
    store.add_edit_plan({"operations": []}, status="pending", rationale=[], generator_id="fixture", generator_version="1")
    config = AppConfig(data_dir=tmp_path / "data")
    before = store.timeline_history()
    assert len(dispatch("proposal_list", {"path": str(store.root)}, config)["plans"]) == 1
    with pytest.raises(ValueError, match="cannot execute"):
        dispatch("proposal_decide", {"path": str(store.root), "planId": written, "suggestionId": "hostile", "decision": "accepted"}, config)
    assert store.timeline_history() == before


def test_planning_review_dispatch_preserves_versioned_arguments(tmp_path, monkeypatch):
    store = ProjectStore.create(tmp_path / "project", "Review")
    captured = {}
    def review(project, plan_id, action, **kwargs):
        captured.update(kwargs)
        assert project.project_id == store.project_id
        assert (plan_id, action) == ("plan", "lock")
        return {"id": plan_id}
    monkeypatch.setattr("localcut_service.server.review_plan", review)
    result = dispatch("planning_review", {"path": str(store.root), "planId": "plan", "action": "lock", "sectionId": "section", "expectedReviewVersion": 4}, AppConfig(data_dir=tmp_path / "data"))
    assert result == {"plan": {"id": "plan"}}
    assert captured["expected_review_version"] == 4
    assert captured["section_id"] == "section"
