"""Command line: seenby check | watch | status."""
from __future__ import annotations

import argparse
import os
import re
import sys

from seenby import __version__, core
from seenby.platforms import PLATFORMS

DEFAULT_LEDGER = "seenby.jsonl"
KEY_ENV = {"colony": "COLONY_API_KEY", "moltbook": "MOLTBOOK_API_KEY"}


def duration(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration: {text!r} (use 90, 90s, 10m, 2h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _line(obs: core.Observation) -> str:
    parts = []
    for v in obs.views:
        seen = "found" if v["found"] else ("error: " + v["error"] if v["error"] else "not found")
        flags = ", ".join(f"{k}={v['flags'][k]}" for k in sorted(v["flags"])) if v["flags"] else ""
        part = f"{v['reader']}: {seen}{' (' + flags + ')' if flags else ''}"
        b = v.get("by_id")
        if b:
            bflags = ", ".join(f"{k}={b['flags'][k]}" for k in sorted(b["flags"]))
            part += "; by id: " + ("found" + (f" ({bflags})" if bflags else "") if b["found"]
                                   else ("error: " + b["error"] if b["error"] else "not found"))
        if obs.sent_sha256 and v.get("body_sha256"):
            part += "; body " + ("matches" if v["body_sha256"] == obs.sent_sha256 else "DIFFERS")
        parts.append(part)
    return f"{obs.ts}  {obs.platform}  {obs.comment_id}  {obs.state.upper()}  [{'; '.join(parts)}]"


def _key(args) -> str | None:
    env = args.author_key_env or KEY_ENV[args.platform]
    return None if args.anonymous_only else (os.environ.get(env) or None)


def _sent(args) -> str | None:
    if not args.body_file:
        return None
    if args.body_file == "-":
        return core.sha256(sys.stdin.read())
    with open(args.body_file, encoding="utf-8") as f:
        return core.sha256(f.read())


def cmd_check(args) -> int:
    platform = PLATFORMS[args.platform]()
    obs = core.observe(platform, args.post, args.comment, author_token=_key(args), sent_sha=_sent(args))
    core.append(args.ledger, obs)
    print(_line(obs))
    return core.exit_code([obs.state])


def cmd_watch(args) -> int:
    platform, key, sent = PLATFORMS[args.platform](), _key(args), _sent(args)
    obs = None
    for obs in core.watch(lambda: core.observe(platform, args.post, args.comment, author_token=key, sent_sha=sent),
                          every=args.every, until_s=args.timeout):
        core.append(args.ledger, obs)
        print(_line(obs), flush=True)
    rows = [r for r in core.status(args.ledger, misses=args.misses, min_gap=args.min_gap)
            if r["comment_id"] == args.comment and r["platform"] == args.platform]
    v = rows[0]["verdict"] if rows else "unknown"
    print(f"verdict: {v.upper()}")
    return core.exit_code([v])


def cmd_status(args) -> int:
    rows = core.status(args.ledger, misses=args.misses, min_gap=args.min_gap)
    if not rows:
        print(f"no observations in {args.ledger}")
        return 3
    print(f"{'platform':9} {'comment':36} {'verdict':12} {'last state':12} {'checks':>6}  last check")
    for r in rows:
        print(f"{r['platform']:9} {r['comment_id']:36} {r['verdict'].upper():12} {r['last_state']:12} "
              f"{r['checks']:>6}  {r['last']}")
    return core.exit_code([r["verdict"] for r in rows])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seenby", description="Check whether a comment you posted is visible to others.")
    ap.add_argument("--version", action="version", version=f"seenby {__version__}")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER, help=f"JSONL ledger of observations (default {DEFAULT_LEDGER})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("check", "read the thread once and record the state"),
                           ("watch", "repeat checks until visible, final, or timeout")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("platform", choices=sorted(PLATFORMS))
        p.add_argument("--post", required=True, help="id of the post the comment belongs to")
        p.add_argument("--comment", required=True, help="id of the comment")
        p.add_argument("--author-key-env", help="env var with the author's API key (default COLONY_API_KEY / "
                                                "MOLTBOOK_API_KEY); without it only the anonymous view is read")
        p.add_argument("--anonymous-only", action="store_true", help="never read as the author")
        p.add_argument("--body-file", help="file with the exact text you sent ('-' for stdin); a different text "
                                           "read back is reported as edited")
        if name == "watch":
            p.add_argument("--every", type=duration, default=duration("60s"), help="time between checks (default 60s)")
            p.add_argument("--timeout", type=duration, default=duration("10m"), help="give up after (default 10m)")
    st = sub.add_parser("status", help="verdict per comment from the ledger")
    for p in (st, sub.choices["watch"]):
        p.add_argument("--misses", type=int, default=2, help="anonymous misses needed to call a comment hidden (2)")
        p.add_argument("--min-gap", type=duration, default=duration("60s"), help="minimum time between misses (60s)")
    args = ap.parse_args(argv)
    return {"check": cmd_check, "watch": cmd_watch, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
