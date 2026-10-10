"""What the workflows and the action must keep, because the repository and its users depend on it.

``main`` requires one check, ``ci-passed``, through a merge queue, and a ``v*`` tag publishes
to PyPI. The action, ``action.yml``, posts on other repositories' behalf. Each test names what
breaks when its rule does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as true
    return data


CI = _workflow("ci.yml")
RELEASE = _workflow("release.yml")
ACTION = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
ACTION_STEPS = {step["name"]: step for step in ACTION["runs"]["steps"]}


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


@pytest.mark.parametrize("file", [*sorted(path.name for path in WORKFLOWS.glob("*.yml")), "action.yml"])
def test_every_action_is_pinned_to_a_commit(file: str) -> None:
    """A tag can be moved to other code; a commit cannot. The publish action is the one exception, below.

    ``./`` is this repository's own action, at the commit being tested.
    """
    if file == "action.yml":
        steps = ACTION["runs"]["steps"]
    else:
        steps = [step for job in _workflow(file)["jobs"].values() for step in job.get("steps", [])]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert uses, "the sweep found the file's actions"
    loose = [
        use
        for use in uses
        if len(use.rsplit("@", 1)[-1]) != 40 and use != "./" and not use.startswith("pypa/gh-action-pypi-publish@")
    ]
    assert loose == []


def test_the_action_hands_its_inputs_to_its_scripts_through_the_environment() -> None:
    """An input written into a script with ${{ }} becomes part of it, so $(...) in a text would run."""
    scripts = [step["run"] for step in ACTION["runs"]["steps"] if "run" in step]
    assert len(scripts) >= 3, "the sweep found the action's scripts"
    assert [script for script in scripts if "${{" in script] == []


def test_the_action_keeps_a_ledger_per_name_and_ref() -> None:
    """A run on a tag or a pull request must not restore the default branch's ledger and post its items again.

    Under a key of its own ref, such a run finds no ledger, so it records what the feed holds and
    posts nothing. A name may not hold a slash, which could make one ledger's keys another's.
    """
    naming = ACTION_STEPS["Name the ledger"]["run"]
    assert 'prefix="herald/$NAME/${GITHUB_REF//,/_}/"' in naming
    assert "^[A-Za-z0-9._-]+$" in naming
    assert ACTION_STEPS["Restore the ledger"]["with"]["restore-keys"] == "${{ steps.ledger.outputs.prefix }}"
    cached = [step["with"] for step in ACTION["runs"]["steps"] if step.get("uses", "").startswith("actions/cache/")]
    assert len(cached) == 3, "the sweep found the restore, the save and the check"
    assert {(step["path"], step["key"]) for step in cached} == {
        ("${{ steps.ledger.outputs.dir }}", "${{ steps.ledger.outputs.key }}")
    }


def test_the_action_saves_the_ledger_whatever_the_watch_did_and_fails_when_it_was_not_saved() -> None:
    """A post the saved ledger does not know of is made again by the next run. Saving only warns when it fails."""
    save = ACTION_STEPS["Save the ledger"]
    check = ACTION_STEPS["Check the ledger was saved, so the next run posts nothing twice"]
    assert ACTION_STEPS["Find the ledger"]["if"] == "always() && steps.watch.outcome != 'skipped'"
    assert save["if"] == check["if"] == "always() && steps.found.outputs.ledger == 'yes'"
    assert save["uses"].startswith("actions/cache/save@")
    assert (check["with"]["lookup-only"], check["with"]["fail-on-cache-miss"]) == ("true", "true")


def test_ci_runs_the_action_on_fresh_runners_that_share_only_the_cache() -> None:
    jobs = CI["jobs"]
    assert (jobs["action-posts"]["needs"], jobs["action-again"]["needs"]) == ("action-records", "action-posts")
    for name in ("action-records", "action-posts", "action-again"):
        [watch] = [step for step in jobs[name]["steps"] if step.get("uses") == "./"]
        assert watch["with"]["ledger"] == "${{ env.LEDGER_NAME }}"
        assert jobs[name]["env"]["LEDGER_NAME"] == "ci-${{ github.run_id }}-${{ github.run_attempt }}"
