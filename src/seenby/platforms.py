"""Thread readers for The Colony and Moltbook. Standard library only.

Both look for the comment in the thread listing, which is what other readers get, not in a single-item
endpoint: a comment can exist by id and still be left out of the thread.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

from seenby import __version__
from seenby.core import View

UA = f"seenby/{__version__}"
FLAG_KEYS = ("is_spam", "is_deleted", "held", "held_explanation", "verification_status")

# fetch(method, url, headers, body) -> (status, parsed json or None); replaceable in tests.
Fetch = Callable[[str, str, dict, dict | None], tuple[int, object]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # an Authorization header must not follow a redirect
        return None


def http_fetch(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 20) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, "Accept": "application/json", **headers,
                                          **({"Content-Type": "application/json"} if data else {})})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw or b"null")
    except ValueError:
        return code, None


def _flags(c: dict) -> dict:
    return {k: c[k] for k in FLAG_KEYS if k in c}


def _scan(pages: Iterator[list[dict]], comment_id: str, reader: str, max_pages: int) -> View:
    n = 0
    try:
        for n, items in enumerate(pages, 1):
            for c in items:
                if str(c.get("id")) == comment_id:
                    return View(reader, True, _flags(c), pages=n)
            if n >= max_pages:
                return View(reader, False, error=f"not found in the first {max_pages} pages", pages=n)
    except RuntimeError as e:
        return View(reader, False, error=str(e), pages=n)
    return View(reader, False, pages=n)


class Colony:
    name = "colony"
    base = "https://thecolony.ai/api/v1"

    def __init__(self, fetch: Fetch = http_fetch, max_pages: int = 50) -> None:
        self.fetch, self.max_pages, self._tokens = fetch, max_pages, {}

    def _auth(self, api_key: str | None) -> dict:
        if not api_key:
            return {}
        if api_key not in self._tokens:
            code, data = self.fetch("POST", f"{self.base}/auth/token", {}, {"api_key": api_key})
            if code != 200 or not isinstance(data, dict) or "access_token" not in data:
                raise RuntimeError(f"auth failed ({code})")
            self._tokens[api_key] = data["access_token"]
        return {"Authorization": f"Bearer {self._tokens[api_key]}"}

    def _pages(self, post_id: str, headers: dict) -> Iterator[list[dict]]:
        page = 1
        while True:
            code, data = self.fetch("GET", f"{self.base}/posts/{urllib.parse.quote(post_id)}/comments?page={page}",
                                    headers, None)
            if code != 200 or not isinstance(data, dict):
                raise RuntimeError(f"GET comments page {page}: {code}")
            yield data.get("items") or []
            if not data.get("has_more"):
                return
            page += 1

    def find(self, post_id: str, comment_id: str, api_key: str | None) -> View:
        reader = "author" if api_key else "anonymous"
        try:
            headers = self._auth(api_key)
        except RuntimeError as e:
            return View(reader, False, error=str(e))
        return _scan(self._pages(post_id, headers), comment_id, reader, self.max_pages)


class Moltbook:
    name = "moltbook"
    base = "https://www.moltbook.com/api/v1"  # without www, the redirect drops the Authorization header

    def __init__(self, fetch: Fetch = http_fetch, max_pages: int = 20) -> None:
        self.fetch, self.max_pages = fetch, max_pages

    @staticmethod
    def flatten(comments: list[dict]) -> list[dict]:
        out = []
        for c in comments:
            out.append(c)
            out.extend(Moltbook.flatten(c.get("replies") or []))
        return out

    def _pages(self, post_id: str, headers: dict) -> Iterator[list[dict]]:
        cursor, n = None, 0
        while True:
            q = {"sort": "new", "limit": "100", **({"cursor": cursor} if cursor else {})}
            url = f"{self.base}/posts/{urllib.parse.quote(post_id)}/comments?{urllib.parse.urlencode(q)}"
            code, data = self.fetch("GET", url, headers, None)
            n += 1
            if code != 200 or not isinstance(data, dict):
                raise RuntimeError(f"GET comments page {n}: {code}")
            yield self.flatten(data.get("comments") or [])
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return

    def find(self, post_id: str, comment_id: str, api_key: str | None) -> View:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return _scan(self._pages(post_id, headers), comment_id, "author" if api_key else "anonymous", self.max_pages)


PLATFORMS = {"colony": Colony, "moltbook": Moltbook}
