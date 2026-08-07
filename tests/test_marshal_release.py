"""wf-164 — marshal stale-claim release policy (seat restore / needs:routing /
trailing completion → propose-close).
"""

from __future__ import annotations

import re

from workforce import marshal_release as mr


def _c(body, cid="1", author="hand"):
    return {"id": cid, "body": body, "author": author}


# --- completion shape -------------------------------------------------------

def test_completed_emdash_trailer_is_completion_shaped():
    """ts-880 class: informal Completed — without full §5 sections."""
    body = (
        "Completed — mockup + screenshots delivered as comment on #849 "
        "(built in the #892 receipt-as-navigator model). "
        "Committed ecb3af8 + c697789."
    )
    assert mr.is_completion_shaped(body)


def test_full_process_sections_are_completion_shaped():
    body = (
        "Completed:\n- foo.py\n\n"
        "Verification:\n- pytest green\n\n"
        "Links:\n- abc1234\n\n"
        "Follow-ups:\n- none\n"
    )
    assert mr.is_completion_shaped(body)


def test_owner_marker_is_not_completion_shaped():
    body = (
        "Owner: salem (grok-4.5)\n"
        "Workdir: /tmp/x\n"
        "Start: 2026-08-04T00:00:00Z\n"
        "Plan:\n- ship it\n"
    )
    assert not mr.is_completion_shaped(body)


def test_blocked_release_is_not_completion_shaped():
    body = (
        "Blocked: marshal released this stale claim after 3h; "
        "last ticket activity 2026-07-01; no live work.\n"
        "Next step: Reclaim from backlog."
    )
    assert not mr.is_completion_shaped(body)


# --- trail tip --------------------------------------------------------------

def test_trailing_completion_wins_over_older_owner():
    comments = [
        _c("Owner: tom\nStart: 2026-07-01T00:00:00Z\nPlan:\n- x", "10", "tom"),
        _c("Completed — shipped mockup on #849", "11", "tom"),
    ]
    tip = mr.trailing_completion_closeout(comments)
    assert tip is not None
    assert tip["id"] == "11"


def test_later_owner_reclaim_clears_completion_tip():
    """If someone re-owned after a Completed comment, do not propose-close."""
    comments = [
        _c("Completed — old deliverable", "1", "hand"),
        _c(
            "Owner: you\nStart: 2026-08-04T00:00:00Z\nPlan:\n- backfill close",
            "2",
            "you",
        ),
    ]
    assert mr.trailing_completion_closeout(comments) is None
    d = mr.decide_release_action(["product:demo"], comments)
    # No worker seat, owner=you → needs:routing (not propose_close)
    assert d["action"] == "stamp_needs_routing"


def test_noise_intake_and_marshal_blocked_skipped():
    comments = [
        _c("Intake: filed by you", "1", "you"),
        _c(
            "Blocked: marshal released this stale claim after 1d; "
            "last ticket activity x; no live work.\nNext step: y",
            "2",
            "marshal",
        ),
        _c("Completed:\n- done\n\nVerification:\n- ok\n\nLinks:\n- a\n\nFollow-ups:\n- none", "3"),
    ]
    tip = mr.most_recent_substantive_comment(comments)
    assert tip is not None and tip["id"] == "3"


# --- decide_release_action --------------------------------------------------

def test_decide_propose_close_ts880_shape():
    """Seatless + trailing Completed — → propose_close (do not re-pool)."""
    labels = ["product:demo"]
    comments = [
        _c(
            "Owner: owner-terminal (claude-fable-5)\n"
            "Start: 2026-07-03T00:38:08Z\nPlan:\n- mockup",
            "1",
            "owner-terminal",
        ),
        _c(
            "Completed — mockup + screenshots delivered as comment on #849 "
            "(commits ecb3af8 + c697789)",
            "2",
            "owner-terminal",
        ),
    ]
    d = mr.decide_release_action(labels, comments)
    assert d["action"] == "propose_close"
    assert d["reason"] == "trailing-completion-comment"
    assert d["stamp_label"] is None


def test_decide_keep_seat_when_worker_label_present():
    labels = ["worker:salem", "product:workforce"]
    comments = [
        _c("Owner: salem\nStart: 2026-08-01T00:00:00Z\nPlan:\n- x", "1", "salem"),
    ]
    d = mr.decide_release_action(labels, comments)
    assert d["action"] == "keep_seat"
    assert d["worker_label"] == "worker:salem"


def test_decide_restore_seat_from_hand_owner():
    labels = ["product:workforce"]  # seat lost / never stamped
    comments = [
        _c("Owner: otto (grok)\nStart: 2026-08-01T00:00:00Z\nPlan:\n- x", "1", "otto"),
        _c("working on it…", "2", "otto"),
    ]
    d = mr.decide_release_action(labels, comments)
    assert d["action"] == "restore_seat"
    assert d["worker_label"] == "worker:otto"
    add, after = mr.apply_label_plan(labels, d)
    assert add == ["worker:otto"]
    assert "worker:otto" in after
    assert "needs:routing" not in after


def test_decide_stamp_needs_routing_seatless_citizen_owner():
    labels = ["product:demo"]
    comments = [
        _c(
            "Owner: owner-terminal\nStart: 2026-07-01T00:00:00Z\nPlan:\n- x",
            "1",
            "owner-terminal",
        ),
        _c("still poking at files", "2", "owner-terminal"),
    ]
    d = mr.decide_release_action(labels, comments)
    assert d["action"] == "stamp_needs_routing"
    assert d["stamp_label"] == "needs:routing"
    add, after = mr.apply_label_plan(labels, d)
    assert add == ["needs:routing"]
    assert "needs:routing" in after


def test_propose_close_precedes_keep_seat():
    """Finished trail wins even when a seat label remains."""
    labels = ["worker:tom"]
    comments = [
        _c("Completed:\n- x\n\nVerification:\n- y\n\nLinks:\n- z\n\nFollow-ups:\n- none", "9"),
    ]
    d = mr.decide_release_action(labels, comments)
    assert d["action"] == "propose_close"
    assert d["worker_label"] == "worker:tom"


def test_is_hand_like_rejects_you_and_paths():
    assert not mr.is_hand_like_owner("you")
    assert not mr.is_hand_like_owner("owner-terminal")
    assert not mr.is_hand_like_owner("marshal")
    assert not mr.is_hand_like_owner("../evil")
    assert not mr.is_hand_like_owner("a@b.com")
    assert mr.is_hand_like_owner("salem")
    assert mr.is_hand_like_owner("chief-of-staff")


# --- bodies -----------------------------------------------------------------

def test_propose_close_body_names_wf164_and_no_completed_header():
    body = mr.propose_close_body(
        age="2 days",
        last_activity="2026-07-03T00:41:27Z",
        completion_excerpt="Completed — mockup delivered",
        decision={"worker_label": None},
    )
    assert body.startswith("Propose-close:")
    assert "wf-164" in body
    assert "Next step:" in body
    # Must not itself be a lifecycle close (marshal never closes).
    assert not body.lstrip().startswith("Completed:")
    assert not re.search(r"(?m)^\s*Verification\s*:", body)



def test_blocked_body_stamp_path_mentions_needs_routing():
    d = {"action": "stamp_needs_routing", "stamp_label": "needs:routing"}
    body = mr.blocked_release_body(
        age="4h",
        last_activity="2026-08-01T00:00:00Z",
        evidence="no live or preservable work was found",
        decision=d,
    )
    assert body.startswith("Blocked:")
    assert "needs:routing" in body
    assert "Next step:" in body


def test_apply_label_plan_idempotent_stamp():
    d = {"action": "stamp_needs_routing", "stamp_label": "needs:routing"}
    add, after = mr.apply_label_plan(["needs:routing", "product:x"], d)
    assert add == []
    assert after == ["needs:routing", "product:x"]


def test_restore_drops_needs_routing():
    d = {
        "action": "restore_seat",
        "worker_label": "worker:otto",
    }
    add, after = mr.apply_label_plan(["needs:routing", "product:x"], d)
    assert add == ["worker:otto"]
    assert "needs:routing" not in after
    assert "worker:otto" in after
