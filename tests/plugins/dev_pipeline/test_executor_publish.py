"""PR idempotency and publish body tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plugins.dev_pipeline import executor as ex


def test_find_existing_pr_returns_number(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    gh = MagicMock(
        return_value=type(
            "P", (), {"returncode": 0, "stdout": '[{"number": 42}]', "stderr": ""}
        )()
    )
    assert ex.find_existing_pr("hermes-dev/t1", repo_dir=repo, gh_fn=gh) == 42
    gh.assert_called_once()
    _args, kwargs = gh.call_args
    assert kwargs.get("cwd") == repo


def test_publish_pr_all_gh_calls_pass_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    recorded: list[tuple[list[str], dict]] = []
    list_calls = {"count": 0}

    def gh(args, **kwargs):
        recorded.append((list(args), dict(kwargs)))
        if args[0:2] == ["pr", "list"]:
            list_calls["count"] += 1
            if list_calls["count"] == 1:
                return type(
                    "P",
                    (),
                    {"returncode": 0, "stdout": '[{"number": 7}]', "stderr": ""},
                )()
            return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if args[0:2] == ["pr", "comment"]:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[0:2] == ["pr", "view"]:
            return type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"url":"https://example/pr/7"}',
                    "stderr": "",
                },
            )()
        if args[0:2] == ["pr", "create"]:
            return type(
                "P",
                (),
                {"returncode": 0, "stdout": "https://example/pr/new", "stderr": ""},
            )()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "unexpected"})()

    git_ok = lambda *_a, **_k: type(
        "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
    )()
    common = dict(
        task_id="t1",
        task_text="task",
        contract={"task_summary": "summary"},
        repo_dir=repo,
        branch="hermes-dev/t1",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="safe diff",
        gh_fn=gh,
        git_fn=git_ok,
    )

    ok, _url, kind = ex.publish_pr(**common)
    assert ok is True
    assert kind == ""
    existing_calls = list(recorded)
    assert any(a[0:2] == ["pr", "comment"] for a, _k in existing_calls)
    assert not any(a[0:2] == ["pr", "create"] for a, _k in existing_calls)
    recorded.clear()

    ok, _url, kind = ex.publish_pr(**common)
    assert ok is True
    create_calls = list(recorded)
    assert any(a[0:2] == ["pr", "create"] for a, _k in create_calls)
    assert not any(a[0:2] == ["pr", "comment"] for a, _k in create_calls)

    for _args, kwargs in existing_calls + create_calls:
        assert kwargs.get("cwd") == repo


def test_find_existing_pr_returns_number_legacy():
    gh = MagicMock(
        return_value=type(
            "P", (), {"returncode": 0, "stdout": '[{"number": 42}]', "stderr": ""}
        )()
    )
    assert ex.find_existing_pr("hermes-dev/t1", gh_fn=gh) == 42


def test_publish_pr_existing_comments_instead_of_create(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def gh(args, **kwargs):
        calls.append(list(args))
        if args[0:2] == ["pr", "list"]:
            return type(
                "P", (), {"returncode": 0, "stdout": '[{"number": 7}]', "stderr": ""}
            )()
        if args[0:2] == ["pr", "comment"]:
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[0:2] == ["pr", "view"]:
            return type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"url":"https://example/pr/7"}',
                    "stderr": "",
                },
            )()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "unexpected"})()

    ok, url, kind = ex.publish_pr(
        task_id="t1",
        task_text="task",
        contract={"task_summary": "summary"},
        repo_dir=repo,
        branch="hermes-dev/t1",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="safe diff",
        gh_fn=gh,
        git_fn=lambda *_a, **_k: type(
            "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    assert ok is True
    assert "7" in url or "example" in url
    assert any(c[0:2] == ["pr", "comment"] for c in calls)
    assert not any(c[0:2] == ["pr", "create"] for c in calls)


def test_publish_pr_creates_when_missing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def gh(args, **kwargs):
        calls.append(list(args))
        if args[0:2] == ["pr", "list"]:
            return type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if args[0:2] == ["pr", "create"]:
            return type(
                "P",
                (),
                {"returncode": 0, "stdout": "https://example/pr/new", "stderr": ""},
            )()
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    ok, url, kind = ex.publish_pr(
        task_id="t99",
        task_text="task",
        contract={"task_summary": "summary"},
        repo_dir=repo,
        branch="hermes-dev/t99",
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
        diff_text="safe diff",
        gh_fn=gh,
        git_fn=lambda *_a, **_k: type(
            "P", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    assert ok is True
    assert any(c[0:2] == ["pr", "create"] for c in calls)


def test_pr_body_contains_job_marker():
    body = ex.build_pr_body(
        task_id="t1",
        task_text="do work",
        contract={"task_summary": "s"},
        lane="cursor-bounded",
        attempt_history=[],
        verification={},
        reviews={},
        evidence_paths=[],
    )
    assert "<!-- hermes-dev-job:t1 -->" in body
