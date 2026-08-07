"""Marshal stale-claim release policy.

Pure helpers for the marshal patrol's narrow release write. Host-neutral:
no desk I/O, no roster reads — callers pass labels + comment trail.

Problems this closes (evidence: ts-880):
1. Release re-pooled seatless tickets (no worker:* , no needs:routing) so
   they never entered triage — Map "No hand" nags.
2. Trailing "Completed —" / §5-shaped comments were treated as silent
   ghosts and released, turning finished work into open unrouted backlog.

Decision order (first match wins):
  propose_close      → most recent substantive comment is completion-shaped
  keep_seat          → ticket already carries worker:*
  restore_seat       → no worker:* but latest Owner: is a hand-like id
  stamp_needs_routing → no seat and no restorable Owner hand

Marshal papers (CONTRACT/prompt) bind the live writes; this module is the
token-free policy + body templates so tests pin the rules without a burn.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Labels that are seats (routing), not area tags.
_WORKER_LABEL_RE = re.compile(r"^worker:(.+)$")

# Owner ids that are never a hireable drain seat for restore purposes.
# Hands are persona slugs; citizen / desk / patrol identities are not.
_NON_HAND_OWNERS = frozenset({
    "you",
    "owner-terminal",
    "founder",
    "marshal",
    "workforce",
    "system",
    "desk",
    "intake",
    "admin",
    "citizen",
    "host",
})

# Ownership marker (PROCESS §5).
_OWNER_MARKER_RE = re.compile(r"(?m)^Owner:\s*([^\s:(]+)")

# Completion / close-out shapes. Lifecycle keys on literal Completed: and
# Verification:; informal trailers also used "Completed —".
_COMPLETED_LINE_RE = re.compile(
    r"(?m)^\s*Completed\s*[:\u2014\u2013\-]",
    re.IGNORECASE,
)
_VERIFICATION_LINE_RE = re.compile(
    r"(?m)^\s*Verification\s*:",
    re.IGNORECASE,
)
_FOLLOWUPS_LINE_RE = re.compile(
    r"(?m)^\s*Follow-ups\s*:",
    re.IGNORECASE,
)
_LINKS_LINE_RE = re.compile(
    r"(?m)^\s*Links\s*:",
    re.IGNORECASE,
)

# Noise comments — not "substantive" for trail inspection.
# Owner: claim markers ARE substantive (a later reclaim after Completed:
# must clear propose-close). Intake / system reconcile notes are not.
_NOISE_BODY_RES = (
    re.compile(r"(?i)^\s*Intake:\s*filed by\b"),
    re.compile(r"(?i)^\s*Blocked:\s*marshal released this stale claim\b"),
    re.compile(r"(?i)^\s*Blocked:\s*Startup reconciliation\b"),
    re.compile(r"(?i)^\s*Blocked:\s*Heartbeat reconciliation\b"),
    re.compile(r"(?i)^\s*Propose-close:\s*"),  # our own prior propose
    re.compile(r"(?i)^\s*Routed:\s*"),
)

ACTIONS = (
    "propose_close",
    "keep_seat",
    "restore_seat",
    "stamp_needs_routing",
)


def worker_seat_labels(labels: Optional[List[Any]]) -> List[str]:
    """Return all worker:<id> labels present (stable order, first = primary)."""
    out: List[str] = []
    for raw in labels or []:
        s = str(raw or "").strip()
        m = _WORKER_LABEL_RE.match(s)
        if m:
            out.append(s)
    return out


def primary_worker_seat(labels: Optional[List[Any]]) -> Optional[str]:
    seats = worker_seat_labels(labels)
    return seats[0] if seats else None


def latest_owner_id(comments: Optional[List[dict]]) -> Optional[str]:
    """Latest ``Owner: <id>`` marker in comment bodies (PROCESS §5 claim)."""
    owner: Optional[str] = None
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        body = str(c.get("body") or "")
        matches = _OWNER_MARKER_RE.findall(body)
        if matches:
            owner = matches[-1].strip()
    return owner or None


def is_hand_like_owner(owner_id: Optional[str]) -> bool:
    """True when *owner_id* is a plausible hireable seat (not citizen/patrol)."""
    if not owner_id:
        return False
    oid = owner_id.strip().lower()
    if not oid or oid in _NON_HAND_OWNERS:
        return False
    # Slug shape: letter start, alnum/hyphen/underscore — reject emails/paths.
    if not re.match(r"^[a-z][a-z0-9_-]{0,63}$", oid):
        return False
    return True


def is_completion_shaped(body: str) -> bool:
    """True when *body* looks like a completion / §5 close-out comment.

    Accepts:
    - Full PROCESS §5 shape (Completed: + Verification: at minimum)
    - Informal ``Completed —`` / ``Completed:`` trailers
    - tp_close-style with Links:/Follow-ups: alongside Completed:
    """
    text = body or ""
    if not text.strip():
        return False
    if _COMPLETED_LINE_RE.search(text):
        # Bare Completed: line is enough for "propose-close" (comment-theater
        # residual). Prefer stronger signal when Verification: co-present.
        return True
    # Defensive: some closers put the word only in a headline.
    if (
        re.search(r"(?i)\bcompleted\b", text)
        and _VERIFICATION_LINE_RE.search(text)
        and (_LINKS_LINE_RE.search(text) or _FOLLOWUPS_LINE_RE.search(text))
    ):
        return True
    return False


def _is_noise_comment(body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return True
    for rx in _NOISE_BODY_RES:
        if rx.search(text):
            # If a noise-shaped prefix somehow coexists with a close-out,
            # keep it substantive so propose-close can still fire.
            if is_completion_shaped(text):
                return False
            return True
    return False


def most_recent_substantive_comment(
    comments: Optional[List[dict]],
) -> Optional[dict]:
    """Newest non-noise comment dict, or None.

    Desk order is usually oldest-first; we scan reversed.
    """
    raw = [c for c in (comments or []) if isinstance(c, dict)]
    for c in reversed(raw):
        body = str(c.get("body") or "")
        if _is_noise_comment(body):
            continue
        return c
    return None


def trailing_completion_closeout(
    comments: Optional[List[dict]],
) -> Optional[dict]:
    """Return the completion-shaped substantive comment if it is the trail tip."""
    tip = most_recent_substantive_comment(comments)
    if tip is None:
        return None
    if is_completion_shaped(str(tip.get("body") or "")):
        return tip
    return None


def decide_release_action(
    labels: Optional[List[Any]],
    comments: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Pick the marshal release branch for one ticket.

    Returns a dict:
      action: propose_close | keep_seat | restore_seat | stamp_needs_routing
      worker_label: optional worker:<id> to keep/restore
      stamp_label: optional label to add (needs:routing)
      reason: short machine-readable cause
      completion_comment_id: when propose_close
    """
    completion = trailing_completion_closeout(comments)
    if completion is not None:
        cid = completion.get("id")
        return {
            "action": "propose_close",
            "worker_label": primary_worker_seat(labels),
            "stamp_label": None,
            "reason": "trailing-completion-comment",
            "completion_comment_id": cid,
        }

    seat = primary_worker_seat(labels)
    if seat is not None:
        return {
            "action": "keep_seat",
            "worker_label": seat,
            "stamp_label": None,
            "reason": "seat-already-present",
            "completion_comment_id": None,
        }

    owner = latest_owner_id(comments)
    if is_hand_like_owner(owner):
        return {
            "action": "restore_seat",
            "worker_label": "worker:%s" % owner.strip().lower(),
            "stamp_label": None,
            "reason": "restore-from-owner",
            "completion_comment_id": None,
        }

    return {
        "action": "stamp_needs_routing",
        "worker_label": None,
        "stamp_label": "needs:routing",
        "reason": "seatless-no-restorable-owner",
        "completion_comment_id": None,
    }


def blocked_release_body(
    *,
    age: str,
    last_activity: str,
    evidence: str,
    decision: Dict[str, Any],
) -> str:
    """PROCESS §5 Blocked:/Next step: body for a ghost release (non-propose)."""
    action = decision.get("action") or "stamp_needs_routing"
    if action == "keep_seat":
        next_step = (
            "Released to backlog on seat %s; reclaim from that ready feed "
            "if work should resume; link any surviving work before continuing."
            % (decision.get("worker_label") or "worker:?")
        )
    elif action == "restore_seat":
        next_step = (
            "Restored seat %s and released to backlog; reclaim from that "
            "ready feed if work should resume."
            % (decision.get("worker_label") or "worker:?")
        )
    else:
        next_step = (
            "Stamped needs:routing and released to backlog — route a hand "
            "(tk label <id> --add worker:<hand>) before reclaiming; do not "
            "leave Map No-hand silent."
        )
    reason = (
        "marshal released this stale claim after %s; last ticket activity %s; "
        "%s"
        % (age, last_activity, evidence)
    )
    return "Blocked: %s\nNext step: %s" % (reason, next_step)


def propose_close_body(
    *,
    age: str,
    last_activity: str,
    completion_excerpt: str,
    decision: Optional[Dict[str, Any]] = None,
) -> str:
    """Comment body when trail already looks finished — do not re-pool open.

    Does not post Completed:/Verification: (marshal never closes). Citizen
    or owning hand verifies and wl_close / structured close.
    """
    excerpt = (completion_excerpt or "").strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:277] + "..."
    seat = (decision or {}).get("worker_label")
    seat_note = (
        " Seat label present: %s." % seat if seat else " No worker:* seat on ticket."
    )
    return (
        "Propose-close: marshal found a trailing completion/close-out comment "
        "on a claim stale after %s (last activity %s). Not releasing to the "
        "open pool — finished work should not reappear as seatless backlog "
        ".%s\n"
        "Trail tip excerpt: %s\n"
        "Next step: Citizen or owning hand verify deliverable and close with "
        "PROCESS §5 Completed:/Verification:/Links:/Follow-ups: (or reopen "
        "with a live Owner: if work remains)."
        % (age, last_activity, seat_note, excerpt or "(empty)")
    )


def apply_label_plan(
    labels: Optional[List[Any]],
    decision: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """Return (labels_to_add, labels_after) for a release (not propose_close).

    propose_close returns ([], current) — no label mutation required.
    """
    current = [str(x) for x in (labels or [])]
    action = decision.get("action")
    if action == "propose_close":
        return [], list(current)
    if action == "keep_seat":
        return [], list(current)
    if action == "restore_seat":
        seat = decision.get("worker_label")
        if not seat or seat in current:
            return [], list(current)
        after = list(current) + [seat]
        # Drop needs:routing if we just seated.
        after = [x for x in after if x != "needs:routing"]
        return [seat], after
    if action == "stamp_needs_routing":
        stamp = decision.get("stamp_label") or "needs:routing"
        if stamp in current:
            return [], list(current)
        return [stamp], list(current) + [stamp]
    return [], list(current)
