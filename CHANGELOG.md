# Changelog

## 0.2.0 (2026-11-05)

- New state `edited`: visible, but not the text you sent. Set when The Colony
  marks an edit (`admin_edited_at`) or, with `--body-file`, when the SHA-256 of
  the body read back differs from the text you sent. The ledger stores hashes
  only.
- The Colony: the anonymous check also reads the comment by id. A listing miss
  with `held: true` by id is reported as `held`; a by-id hit missing from the
  listing is the new `unlisted` state (a miss, like `absent`); past the page
  limit the by-id read decides instead of an `error`.
- Both ideas came from feedback on the first release.

## 0.1.0 (2026-11-04)

- First release: anonymous and author reads of the thread listing on The Colony
  and Moltbook, states, JSON Lines ledger, `check`, `watch`, `status`, and the
  two-miss rule.
