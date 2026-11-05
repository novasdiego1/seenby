import json

import pytest

from seenby import cli, core
from seenby.core import View, classify, verdict
from seenby.platforms import Colony, Moltbook

CID = "c-1"


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("anon,author,expected", [
    (View("anonymous", True, {}), None, "visible"),
    (View("anonymous", True, {"verification_status": "verified", "is_spam": False}), None, "visible"),
    (View("anonymous", True, {"verification_status": "pending"}), None, "pending"),
    (View("anonymous", True, {"verification_status": "failed"}), None, "failed"),
    (View("anonymous", True, {"held": True}), None, "held"),
    (View("anonymous", True, {"is_deleted": True, "verification_status": "pending"}), None, "deleted"),
    (View("anonymous", False), View("author", True, {"verification_status": "verified", "is_spam": True}), "spam"),
    (View("anonymous", False), View("author", True, {"verification_status": "verified"}), "author-only"),
    (View("anonymous", False), View("author", False), "absent"),
    (View("anonymous", False), None, "absent"),
    (View("anonymous", False, error="503"), None, "error"),
    (View("anonymous", False, error="503"), View("author", True, {}), "author-only"),
    (View("anonymous", False), View("author", False, error="auth failed"), "error"),
])
def test_classify(anon, author, expected):
    assert classify(anon, author) == expected


# ---------------------------------------------------------------- verdict (two-miss rule)

def rows(*pairs):
    return [{"ts": f"2026-11-05T00:{m:02d}:00Z", "state": s} for m, s in pairs]


def test_one_miss_is_unconfirmed():
    assert verdict(rows((0, "absent"))) == "unconfirmed"


def test_two_spaced_misses_are_hidden():
    assert verdict(rows((0, "absent"), (2, "author-only"))) == "hidden"


def test_misses_too_close_do_not_count():
    r = [{"ts": "2026-11-05T00:00:00Z", "state": "absent"}, {"ts": "2026-11-05T00:00:30Z", "state": "absent"}]
    assert verdict(r, min_gap=60) == "unconfirmed"


def test_a_hit_resets_the_run():
    assert verdict(rows((0, "absent"), (2, "visible"), (4, "absent"))) == "unconfirmed"


def test_errors_are_ignored_and_latest_wins():
    assert verdict(rows((0, "pending"), (2, "visible"), (3, "error"))) == "visible"
    assert verdict(rows((0, "error"))) == "unknown"


def test_exit_codes():
    assert core.exit_code(["visible"]) == 0
    assert core.exit_code(["visible", "pending"]) == 3
    assert core.exit_code(["unconfirmed"]) == 3
    assert core.exit_code(["pending", "spam"]) == 1
    assert core.exit_code(["hidden"]) == 1


# ---------------------------------------------------------------- platforms with fake HTTP

class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        for (m, needle, auth), resp in self.routes.items():
            if m == method and needle in url and (auth is None or (("Authorization" in headers) == auth)):
                return resp
        return 404, None


def test_colony_paginates_and_reads_held():
    fake = FakeHTTP({
        ("GET", "page=1", None): (200, {"items": [{"id": "x"}], "has_more": True}),
        ("GET", "page=2", None): (200, {"items": [{"id": CID, "held": True, "held_explanation": "review"}],
                                        "has_more": False}),
    })
    v = Colony(fetch=fake).find("p", CID, None)
    assert v.found and v.flags == {"held": True, "held_explanation": "review"} and v.pages == 2
    assert all("Authorization" not in h for _, _, h, _ in fake.calls)


def test_colony_author_view_exchanges_key_once():
    fake = FakeHTTP({
        ("POST", "/auth/token", None): (200, {"access_token": "T"}),
        ("GET", "page=1", True): (200, {"items": [{"id": CID}], "has_more": False}),
    })
    col = Colony(fetch=fake)
    col.find("p", CID, "key")
    col.find("p", CID, "key")
    assert [c[0] for c in fake.calls].count("POST") == 1
    assert fake.calls[1][2]["Authorization"] == "Bearer T"


def test_colony_bad_key_is_an_error_not_absent():
    fake = FakeHTTP({("POST", "/auth/token", None): (401, {"detail": "no"})})
    v = Colony(fetch=fake).find("p", CID, "bad")
    assert not v.found and "auth failed" in v.error


def test_moltbook_walks_reply_tree_and_cursor():
    tree = [{"id": "a", "replies": [{"id": "b", "replies": [{"id": CID, "verification_status": "pending",
                                                              "is_spam": False, "replies": []}]}]}]
    fake = FakeHTTP({
        ("GET", "cursor=K", None): (200, {"comments": tree, "has_more": False}),
        ("GET", "sort=new", None): (200, {"comments": [{"id": "z", "replies": []}], "has_more": True,
                                          "next_cursor": "K"}),
    })
    v = Moltbook(fetch=fake).find("p", CID, None)
    assert v.found and v.flags == {"verification_status": "pending", "is_spam": False}
    assert all(u.startswith("https://www.moltbook.com/") for _, u, _, _ in fake.calls)


def test_page_cap_reports_error_instead_of_absent():
    fake = FakeHTTP({("GET", "page=", None): (200, {"items": [{"id": "x"}], "has_more": True})})
    v = Colony(fetch=fake, max_pages=3).find("p", CID, None)
    assert not v.found and v.error and v.pages == 3
    assert classify(v, None) == "error"


def test_http_error_is_error():
    v = Moltbook(fetch=FakeHTTP({("GET", "sort=new", None): (429, None)})).find("p", CID, None)
    assert v.error and classify(v, None) == "error"


# ---------------------------------------------------------------- watch and CLI

def test_watch_stops_when_visible():
    states = iter(["pending", "pending", "visible", "visible"])
    seen = list(core.watch(lambda: core.Observation("t", "x", "p", CID, next(states), []), every=1, until_s=100,
                           sleep=lambda s: None, clock=lambda: 0))
    assert [o.state for o in seen] == ["pending", "pending", "visible"]


def test_watch_times_out():
    t = iter(range(0, 1000, 30))
    seen = list(core.watch(lambda: core.Observation("t", "x", "p", CID, "absent", []), every=30, until_s=100,
                           sleep=lambda s: None, clock=lambda: next(t)))
    assert len(seen) == 3


def test_cli_check_and_status(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "l.jsonl"
    fake = FakeHTTP({("GET", "sort=new", None): (200, {"comments": [{"id": CID, "verification_status": "verified",
                                                                    "is_spam": False, "replies": []}],
                                                       "has_more": False})})
    monkeypatch.setitem(cli.PLATFORMS, "moltbook", lambda: Moltbook(fetch=fake))
    monkeypatch.delenv("MOLTBOOK_API_KEY", raising=False)
    assert cli.main(["--ledger", str(ledger), "check", "moltbook", "--post", "p", "--comment", CID]) == 0
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["state"] == "visible" and row["views"][0]["reader"] == "anonymous"
    assert cli.main(["--ledger", str(ledger), "status"]) == 0
    assert "VISIBLE" in capsys.readouterr().out


def test_cli_never_sends_key_with_anonymous_only(tmp_path, monkeypatch):
    fake = FakeHTTP({("GET", "page=1", None): (200, {"items": [], "has_more": False})})
    monkeypatch.setitem(cli.PLATFORMS, "colony", lambda: Colony(fetch=fake))
    monkeypatch.setenv("COLONY_API_KEY", "secret")
    code = cli.main(["--ledger", str(tmp_path / "l.jsonl"), "check", "colony", "--post", "p", "--comment", CID,
                     "--anonymous-only"])
    assert code == 3  # absent once = unconfirmed, not visible
    assert all(c[0] == "GET" and "Authorization" not in c[2] for c in fake.calls)


def test_duration():
    assert cli.duration("90") == 90 and cli.duration("10m") == 600 and cli.duration("2h") == 7200
