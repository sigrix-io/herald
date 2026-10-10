"""The JSON-lines ledger: what stands for each post, a crash's leftovers, and one claim at a time."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from herald import Draft, DraftItem, Entry, JsonLinesLedger, LedgerBusy, default_ledger_dir
from herald.ledger import fold


def _entry(state: str, channel: str = "fake", at: str = "2026-10-10T09:00:00+00:00", **extra: str) -> Entry:
    return Entry("v1", channel, state, "d1", at, **extra)  # type: ignore[arg-type]


@pytest.mark.parametrize("held", ["sending", "posted"])
def test_a_preview_never_moves_a_post_made_or_under_way(held: str) -> None:
    standing = fold([_entry("drafted"), _entry(held), _entry("drafted", at="later")])
    assert standing[("v1", "fake")].state == held


@pytest.mark.parametrize("earlier", ["drafted", "failed"])
def test_a_preview_moves_a_draft_or_a_refusal(earlier: str) -> None:
    standing = fold([_entry(earlier), _entry("drafted", at="later")])
    assert (standing[("v1", "fake")].state, standing[("v1", "fake")].at) == ("drafted", "later")


def test_the_entries_stand_in_the_order_they_last_moved(ledger: JsonLinesLedger) -> None:
    for entry in (_entry("drafted", "a"), _entry("drafted", "b"), _entry("posted", "a"), _entry("drafted", "b")):
        ledger.record(entry)
    assert [(entry.channel, entry.state) for entry in ledger.latest()] == [("a", "posted"), ("b", "drafted")]
    ledger.record(_entry("drafted", "a"))
    assert [entry.channel for entry in ledger.latest()] == ["a", "b"], "a draft that changed nothing moved nothing"


def test_a_last_line_cut_short_is_a_write_that_never_happened(ledger: JsonLinesLedger, ledger_dir) -> None:
    ledger.record(_entry("sending"))
    path = ledger_dir / "ledger.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(_entry("posted")))[:30])

    assert ledger.state("v1", "fake").state == "sending"
    ledger.record(_entry("failed"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["state"] for line in lines] == ["sending", "failed"]


def test_a_broken_line_in_the_middle_is_named(ledger: JsonLinesLedger, ledger_dir) -> None:
    ledger.record(_entry("drafted"))
    path = ledger_dir / "ledger.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n" + json.dumps(asdict(_entry("posted"))) + "\n")

    with pytest.raises(ValueError, match=r"ledger\.jsonl line 2 is not JSON"):
        ledger.state("v1", "fake")


def test_a_lock_left_behind_is_reported_with_what_to_do(ledger_dir) -> None:
    ledger_dir.mkdir()
    (ledger_dir / "ledger.lock").write_text("12345\n")

    with pytest.raises(LedgerBusy, match="a crash left it behind: delete it"):
        JsonLinesLedger(ledger_dir, lock_timeout=0.1).record(_entry("drafted"))


def test_of_many_claims_on_one_post_exactly_one_wins(ledger_dir) -> None:
    results: list[Entry | None] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait()
        results.append(JsonLinesLedger(ledger_dir).claim(_entry("sending")))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(None) == 1
    assert {entry.state for entry in results if entry is not None} == {"sending"}
    assert not (ledger_dir / "ledger.lock").exists()


def test_a_claim_after_a_refusal_or_a_draft_goes_through(ledger: JsonLinesLedger) -> None:
    ledger.record(_entry("failed"))
    assert ledger.claim(_entry("sending")) is None
    assert ledger.claim(_entry("sending", at="later")).at == "2026-10-10T09:00:00+00:00"


def test_a_field_a_later_version_adds_is_read_past(ledger: JsonLinesLedger, ledger_dir) -> None:
    ledger_dir.mkdir()
    record = {**asdict(_entry("posted", url="https://fake.example/1")), "attempt": 2}
    (ledger_dir / "ledger.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    draft = {
        "id": "d1",
        "announcement_id": "v1",
        "created_at": "now",
        "items": [{"channel": "fake", "text": "Hello", "count": 5, "limit": 40, "weight": 1}],
    }
    (ledger_dir / "drafts.jsonl").write_text(json.dumps(draft) + "\n", encoding="utf-8")

    assert ledger.state("v1", "fake").url == "https://fake.example/1"
    assert ledger.load_draft("d1").items == (DraftItem("fake", "Hello", 5, 40),)


def test_a_draft_comes_back_as_it_was_kept(ledger: JsonLinesLedger) -> None:
    draft = Draft("d1", "v1", (DraftItem("fake", "Hello 👋🏽", 7, 40), DraftItem("stdout", "Hi", 2, None)), "now")
    ledger.save_draft(draft)
    assert ledger.load_draft(" d1 ") == draft
    with pytest.raises(KeyError):
        ledger.load_draft("d2")


def test_the_default_place_follows_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERALD_LEDGER_DIR", str(tmp_path / "explicit"))
    assert default_ledger_dir() == tmp_path / "explicit"
    monkeypatch.setenv("HERALD_LEDGER_DIR", " ")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_ledger_dir() == tmp_path / "state" / "herald"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert default_ledger_dir() == tmp_path / "home" / ".local" / "state" / "herald"
