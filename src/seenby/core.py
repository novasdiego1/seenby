"""seenby: did anyone else actually see the comment you posted?

A write API that answers 200/201 only says the server accepted the comment. Whether other readers can see it
is a separate fact: the comment may be pending a challenge, flagged as spam, held for moderation, deleted,
or visible only to its author. seenby reads the thread twice, as an anonymous reader and (optionally) as the
author, compares the two, and keeps a dated ledger so a later change is a new observation, not an overwrite.

States of one observation:
  visible       an anonymous reader gets the comment with no hiding flag
  pending       the platform says it awaits verification (Moltbook challenge not solved yet)
  failed        the verification challenge failed: it will not be published
  spam          flagged as spam
  held          held for moderation (The Colony)
  deleted       marked deleted
  author-only   the author sees it, an anonymous reader does not, and no flag says why
  absent        nobody who was asked sees it
  error         the read itself failed (network, rate limit, bad response)
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import pairwise

HIDING = ("deleted", "spam", "held", "failed", "pending")  # order = precedence when several flags are set
BAD = {"failed", "spam", "held", "deleted", "author-only", "hidden"}


@dataclass
class View:
    """What one reader got: whether the comment was found and the flags it carried."""
    reader: str  # "anonymous" | "author"
    found: bool
    flags: dict = field(default_factory=dict)
    error: str | None = None
    pages: int = 0


@dataclass
class Observation:
    ts: str
    platform: str
    post_id: str
    comment_id: str
    state: str
    views: list[dict]


def flag_state(flags: dict) -> str | None:
    """The hiding state the flags point to, or None if they say the comment is public."""
    present = {
        "deleted": bool(flags.get("is_deleted")),
        "spam": bool(flags.get("is_spam")),
        "held": bool(flags.get("held")),
        "failed": flags.get("verification_status") == "failed",
        "pending": flags.get("verification_status") == "pending",
    }
    return next((s for s in HIDING if present[s]), None)


def classify(anon: View, author: View | None) -> str:
    if anon.error and (author is None or author.error):
        return "error"
    if anon.found:
        return flag_state(anon.flags) or "visible"
    if author is not None and author.found:
        return flag_state(author.flags) or "author-only"
    if anon.error or (author is not None and author.error):
        return "error"  # one reader failed and the other did not find it: not enough to say absent
    return "absent"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def observe(platform, post_id: str, comment_id: str, *, author_token: str | None = None) -> Observation:
    """platform: an object with find(post_id, comment_id, token) -> View (see seenby.platforms)."""
    anon = platform.find(post_id, comment_id, None)
    author = platform.find(post_id, comment_id, author_token) if author_token else None
    views = [anon] + ([author] if author else [])
    return Observation(now_iso(), platform.name, post_id, comment_id, classify(anon, author),
                       [asdict(v) for v in views])


# ---------------------------------------------------------------- ledger

def append(path: str, obs: Observation) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(obs), ensure_ascii=False) + "\n")


def read(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _seconds(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def verdict(rows: list[dict], *, misses: int = 2, min_gap: float = 60.0) -> str:
    """Verdict for one comment from its observations, oldest first.

    The latest non-error observation decides, except that "absent" and "author-only" only become "hidden" after
    `misses` anonymous misses at least `min_gap` seconds apart with no anonymous hit after the first of them;
    before that they are "unconfirmed" (one miss can be a cache or replication delay)."""
    rows = [r for r in rows if r["state"] != "error"]
    if not rows:
        return "unknown"
    last = rows[-1]["state"]
    if last not in ("absent", "author-only"):
        return last
    run = []
    for r in reversed(rows):
        if r["state"] not in ("absent", "author-only"):
            break
        run.append(_seconds(r["ts"]))
    run.reverse()
    spaced = [run[0]]
    for t in run[1:]:
        if t - spaced[-1] >= min_gap:
            spaced.append(t)
    return "hidden" if len(spaced) >= misses else "unconfirmed"


def status(path: str, *, misses: int = 2, min_gap: float = 60.0) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in read(path):
        groups.setdefault((r["platform"], r["comment_id"]), []).append(r)
    out = []
    for (platform, cid), rows in groups.items():
        changes = [r["ts"] for a, r in pairwise(rows) if a["state"] != r["state"]]
        out.append({"platform": platform, "comment_id": cid, "post_id": rows[-1]["post_id"],
                    "verdict": verdict(rows, misses=misses, min_gap=min_gap), "last_state": rows[-1]["state"],
                    "first": rows[0]["ts"], "last": rows[-1]["ts"], "checks": len(rows),
                    "last_change": changes[-1] if changes else None})
    return out


def exit_code(verdicts: list[str]) -> int:
    """1 if any comment is not reaching readers, 3 if any is still undecided, 0 if all are visible."""
    if any(v in BAD for v in verdicts):
        return 1
    if any(v != "visible" for v in verdicts):
        return 3
    return 0


def watch(check: Callable[[], Observation], *, every: float, until_s: float,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic
          ) -> Iterator[Observation]:
    """Repeat a check until the comment is visible, reaches a final hiding state, or `until_s` passes."""
    start = clock()
    while True:
        obs = check()
        yield obs
        if obs.state in ("visible", "failed", "spam", "deleted") or clock() - start + every > until_s:
            return
        sleep(every)
