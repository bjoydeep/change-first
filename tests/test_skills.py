from app.skills.pr_basics import (
    _check_diff_size,
    _check_draft_targeting_protected_branch,
    _check_pr_description,
)
from app.skills import get_enabled_skills


# _check_pr_description
def test_description_ok():
    assert "OK" in _check_pr_description("This PR fixes the login bug by updating the auth middleware.", "Fix login")


def test_description_missing():
    assert "ERROR" in _check_pr_description(None, "Fix login")


def test_description_too_short():
    assert "ERROR" in _check_pr_description("short", "Fix")


def test_description_empty_string():
    assert "ERROR" in _check_pr_description("", "Fix")


# _check_diff_size
def test_diff_size_ok():
    assert "OK" in _check_diff_size(50, 20, 3)


def test_diff_size_warning():
    assert "WARNING" in _check_diff_size(900, 200, 35)


def test_diff_size_caution():
    assert "CAUTION" in _check_diff_size(400, 150, 10)


def test_diff_size_boundary_lines():
    assert "WARNING" in _check_diff_size(800, 201, 5)


# _check_draft_targeting_protected_branch
def test_draft_ok():
    assert "OK" in _check_draft_targeting_protected_branch(False, "main")


def test_draft_warning():
    assert "WARNING" in _check_draft_targeting_protected_branch(True, "main")


def test_draft_feature_branch_ok():
    assert "OK" in _check_draft_targeting_protected_branch(True, "feature/foo")


def test_draft_master_warning():
    assert "WARNING" in _check_draft_targeting_protected_branch(True, "master")


# skill registry
def test_get_enabled_skills_all():
    assert len(get_enabled_skills("all")) == 3


def test_get_enabled_skills_subset():
    assert len(get_enabled_skills("check_pr_description,check_diff_size")) == 2


def test_get_enabled_skills_unknown_ignored():
    assert len(get_enabled_skills("check_pr_description,nonexistent_skill")) == 1


def test_get_enabled_skills_empty():
    assert get_enabled_skills("") == []
