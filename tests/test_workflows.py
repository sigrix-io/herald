"""What the workflows must keep, because the repository's settings depend on it.

``main`` requires one check, ``ci-passed``, through a merge queue, and a ``v*`` tag publishes
to PyPI. Each test names what breaks when its rule does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as true
    return data


CI = _workflow("ci.yml")
RELEASE = _workflow("release.yml")


def test_ci_runs_for_the_merge_queue() -> None:
    """Without it, every queued pull request waits on a check that never arrives."""
    assert "merge_group" in CI["on"]
    assert "pull_request" in CI["on"]


def test_queue_entries_do_not_share_a_concurrency_key() -> None:
    """A merge group has no pull request number; a key built from it alone cancels each entry in turn."""
    group = CI["concurrency"]["group"]
    assert "github.event.pull_request.number || github.ref" in group
    assert CI["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_ci_passed_reports_whatever_happens_to_every_other_job() -> None:
    gate = CI["jobs"]["ci-passed"]
    assert gate["name"] == "ci-passed", "the ruleset requires the check by this name"
    assert gate["if"] == "always()", "a required check that is skipped never reports"
    assert sorted(gate["needs"]) == sorted(job for job in CI["jobs"] if job != "ci-passed")


def test_ci_passed_fails_unless_every_job_succeeded() -> None:
    [step] = CI["jobs"]["ci-passed"]["steps"]
    assert step["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
    assert '[ "$result" != "success" ]' in step["run"]
    assert "exit 1" in step["run"]


def test_a_release_comes_only_from_a_version_tag() -> None:
    assert RELEASE["on"] == {"push": {"tags": ["v*"]}}
    check = next(step for step in RELEASE["jobs"]["build"]["steps"] if step.get("name", "").startswith("The tag"))
    assert '"$GITHUB_REF_NAME" != "v$version"' in check["run"]


def test_only_the_publish_job_can_mint_a_pypi_token() -> None:
    """Trusted publishing: PyPI trusts this file and the pypi environment, so the token stays in that job."""
    assert RELEASE["permissions"] == {"contents": "read"}
    publish = RELEASE["jobs"]["publish"]
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert publish["needs"] == "build"
    assert all("permissions" not in job for name, job in RELEASE["jobs"].items() if name != "publish")


@pytest.mark.parametrize("workflow", sorted(path.name for path in WORKFLOWS.glob("*.yml")))
def test_every_action_is_pinned_to_a_commit(workflow: str) -> None:
    """A tag can be moved to other code; a commit cannot. The publish action is the one exception, below."""
    steps = [step for job in _workflow(workflow)["jobs"].values() for step in job.get("steps", [])]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert uses, "the sweep found the workflow's actions"
    loose = [
        use for use in uses if len(use.rsplit("@", 1)[-1]) != 40 and not use.startswith("pypa/gh-action-pypi-publish@")
    ]
    assert loose == []
