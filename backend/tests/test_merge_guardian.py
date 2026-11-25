"""Regression fixtures for the trusted merge policy; no network or real tokens."""

import copy
from unittest.mock import Mock

import pytest

from tools.merge_guardian import evaluate, event_revision, reviewer_approved, run_pr, sensitive

REPO = "owner/repo"
A = "a" * 40
B = "b" * 40
ORDINARY = {"filename": "backend/modules/processing/classifier.py", "status": "modified"}
SENSITIVE = {"filename": ".github/workflows/merge-guardian.yml", "status": "modified"}


def pr(sha=A):
    return {
        "number": 3,
        "head": {"sha": sha},
        "base": {"ref": "main", "sha": "c" * 40, "repo": {"full_name": REPO}},
        "changed_files": 1,
        "state": "open",
        "draft": False,
        "user": {"login": "author"},
    }


def approval(sha=A):
    return {
        "id": 1, "user": {"login": "reviewer", "type": "User"},
        "state": "APPROVED", "commit_id": sha, "author_association": "COLLABORATOR",
    }


def test_routine_needs_no_human():
    reviews = Mock()
    assert evaluate(lambda: pr(), lambda: [ORDINARY], reviews, A, REPO) == "NOT_REQUIRED"
    reviews.assert_not_called()


@pytest.mark.parametrize("files,count", [
    ([], 0), ([ORDINARY], 2), ([SENSITIVE], 1),
    ([ORDINARY | {"status": "renamed", "previous_filename": "backend/core/auth.py"}], 1),
    ([ORDINARY | {"status": "removed"}], 1),
])
def test_sensitive_paths_fail_closed(files, count):
    assert sensitive(files, count)


@pytest.mark.parametrize("review", [
    approval(B),
    approval() | {"user": {"login": "AUTHOR", "type": "User"}},
    approval() | {"user": {"login": "bot", "type": "Bot"}},
    approval() | {"author_association": "NONE"},
])
def test_unqualified_review(review):
    assert not reviewer_approved(pr(), [review])


def test_sensitive_current_review():
    assert evaluate(lambda: pr(), lambda: [SENSITIVE], lambda: [approval()], A, REPO) == "PASS"


def test_sensitive_missing_review():
    assert evaluate(lambda: pr(), lambda: [SENSITIVE], lambda: [], A, REPO) == "MISSING"


def test_head_drift_before_evaluation():
    files = Mock()
    with pytest.raises(ValueError, match="revision"):
        evaluate(lambda: pr(B), files, Mock(), A, REPO)
    files.assert_not_called()


def test_head_drift_during_evaluation():
    with pytest.raises(ValueError, match="changed"):
        evaluate(Mock(side_effect=[pr(), pr(B)]), lambda: [SENSITIVE],
                 lambda: [approval()], A, REPO)


def test_head_drift_at_final_check():
    with pytest.raises(ValueError, match="before reporting"):
        evaluate(Mock(side_effect=[pr(), pr(), pr(B)]), lambda: [SENSITIVE],
                 lambda: [approval()], A, REPO)


def test_base_drift_during_evaluation():
    changed = copy.deepcopy(pr())
    changed["base"]["sha"] = B
    with pytest.raises(ValueError, match="changed"):
        evaluate(Mock(side_effect=[pr(), changed]), lambda: [ORDINARY], Mock(), A, REPO)


def test_review_dismissed_during_evaluation():
    reviews = Mock(side_effect=[[approval()], [approval() | {"state": "DISMISSED"}]])
    with pytest.raises(ValueError, match="Reviews changed"):
        evaluate(lambda: pr(), lambda: [SENSITIVE], reviews, A, REPO)


def test_eligible_objection_blocks_approval():
    objection = approval() | {"id": 2, "state": "CHANGES_REQUESTED",
                             "user": {"login": "other", "type": "User"}}
    assert not reviewer_approved(pr(), [approval(), objection])


def test_event_revision_uses_original_payload():
    assert event_revision("pull_request_target", {"pull_request": pr()}) == (3, A)
    with pytest.raises(ValueError, match="Untrusted"):
        event_revision("pull_request_review", {})


def test_recheck_requires_exact_sha_and_eligible_user():
    payload = {"issue": {"number": 3, "pull_request": {}},
               "comment": {"body": f"/guardian-recheck {A}",
                           "author_association": "OWNER", "user": {"type": "User"}}}
    assert event_revision("issue_comment", payload) == (3, A)
    payload["comment"]["author_association"] = "NONE"
    assert event_revision("issue_comment", payload) is None


def test_stale_event_cannot_publish_status(monkeypatch):
    monkeypatch.setattr("tools.merge_guardian.api", lambda path: pr(B))
    publish = Mock()
    monkeypatch.setattr("tools.merge_guardian.status", publish)
    with pytest.raises(ValueError):
        run_pr(REPO, "pull_request_target", {"pull_request": pr()})
    publish.assert_not_called()


def test_api_failure_marks_exact_head_failed(monkeypatch):
    monkeypatch.setattr("tools.merge_guardian.api", lambda path: pr())
    monkeypatch.setattr("tools.merge_guardian.inspect", Mock(side_effect=RuntimeError("API")))
    publish = Mock()
    monkeypatch.setattr("tools.merge_guardian.status", publish)
    with pytest.raises(RuntimeError):
        run_pr(REPO, "pull_request_target", {"pull_request": pr()})
    assert [(call.args[1], call.args[2]) for call in publish.call_args_list] == [
        (A, "pending"), (A, "failure"),
    ]
