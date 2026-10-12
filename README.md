# mia-snippets

Immutable "snippet" readers for microscopy data formats, plus a human-owned `harness` package
(`smoke`, `crop`, `verify`, `preview`) that samples 256×256 PNG crops through them. See `SPEC.md`.

Snippets are immutable once merged and are human-approved; a wrong one is withdrawn in `WITHDRAWN.yaml`, never edited.

Harness tests: `cd harness && pixi run test`.

CI rules: `ci/check_pr.py` (stdlib + pyyaml) runs from `.github/workflows/check.yml`; its tests run with
`cd harness && pixi run ci-test`. Maintainers (who may change anything outside `snippets/`) are listed in
`ci/maintainers.txt`; the snippet `requires` allowlist is `harness/allowlist.txt`. CI runs the checker,
the maintainers and the allowlist as they are on `main` (`--base origin/main`), so a PR cannot loosen its own
check by editing `ci/` or the allowlist; such edits take effect from the next PR. Changes to `.github/` itself
(including the workflow that does this) remain guarded only by the required review and autodev's refusal of
agent commits that touch `.github/`.

## Branch protection

Set on `main` once the repo exists (not applied yet):
- require the status checks `check` and `mia-data/crops` (the latter restricted to the mia-data app, once it exists);
- require one approving review; dismiss stale approvals.

PRs that touch nothing under `snippets/` get `mia-data/crops` = success from the poller. Until the poller
runs, merge maintainer PRs by review alone, before protection is switched on.
