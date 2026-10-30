# mia-snippets

Immutable **snippets** that read microscopy data formats, plus a human-owned **harness** that samples 256×256
PNG crops through them. `SPEC.md` is the contract; this README explains it. The mia-data worker (in
`temporal-prototype`) drives everything: it matches datasets to snippets, asks autodev to write a snippet when
none fits, tests the result on real data, and crops in production once a human approves.

## What a snippet is

One directory, `snippets/<name>/`, added once and never changed:

```
snippets/<name>/
├── snippet.py                  SPEC_VERSION = 1 and open_array(src: Source, io: IO) -> Array
├── snippet.yaml                name, formats, containers, params schema, requires, tested_on, supersedes
├── README.md                   what it reads, how params are chosen, known limits
└── examples/<record-id>.yaml   ≥ 1 binding it was tested on
```

`snippet.py` does one thing: given a `Source` (file URLs and params from a binding) it returns an `Array`, an
object with `axes` (letters from `czyx`), `shape`, `dtype`, `voxel_size_nm`, `access` (`chunk`, `plane` or
`file`: the cheapest unit this file can be read in) and `read(region)`. It must not read voxel data until
`read` is called. Everything else, sampling, filtering, stretching to PNG, overlays, provenance, is the
harness's job, so every crop in the system goes through the same code.

Rules a snippet lives by (SPEC §2, §3):

- **Type hints are required.** `from mia_harness.api import IO, Array, Source` and annotate `open_array`
  exactly as above. CI rejects an unannotated `open_array`. (`tiff-zip-member` and `mrc-http` predate this rule
  and stay as tested; immutability wins over tidiness.) `mia_harness.api` is the only harness module a
  snippet may import; it also exports the exceptions `NeedsFetch`, `HostNotAllowed` and `BudgetExceeded`.
- **All bytes come through `io`.** `io.open(url)` is a seekable range-backed file, `io.fetch(url)` stages the
  whole file, `io.list(url)` lists zip members. `io` enforces the job's byte cap and host allowlist and
  records validators for provenance. A snippet never uses `requests`, `urllib`, `fsspec`, sockets,
  subprocesses, `pickle`, or writes outside `io`'s staging.
- **Pure, independent reader.** No cropping, resampling or normalising. No imports of other snippets; shared
  code becomes a new snippet. Dependencies must be on `harness/allowlist.txt`; an agent that needs a new one
  reports it, and a human extends the harness environment.
- **Immutable.** A fix is a new directory (`tiff-zip-member-2`) whose `snippet.yaml` sets `supersedes`. A
  wrong snippet is listed in `WITHDRAWN.yaml` by a maintainer, never edited or deleted (SPEC §6).

A **binding** (SPEC §4) is the data that connects a dataset to snippets: which snippet reads which URLs with
which params, how labels pair with raws, and a reason for every non-default choice. Agents write bindings;
humans approve them.

## How conformance is checked

Four layers, cheapest first. A snippet reaches production only by passing all of them.

1. **Static CI on every PR** (`ci/check_pr.py`, run by `.github/workflows/check.yml`). A PR may only add
   files under one new `snippets/<name>/`; changing anything existing fails unless the author is in
   `ci/maintainers.txt`. `snippet.yaml` must validate: name matches the directory and is unused, every
   `requires` is on the allowlist, every `tested_on` has an example. `snippet.py` is parsed, not run: banned
   modules and calls, `allow_pickle`, imports of other snippets or of harness internals, a literal
   `SPEC_VERSION = 1`, and a fully annotated `open_array`. CI runs the checker, maintainers and allowlist as
   they are on `main` (`--base origin/main`), so a PR cannot loosen its own check.
2. **The loader, before any snippet code runs** (`harness/src/mia_harness/loader.py`). It refuses a
   withdrawn or superseded snippet, a `spec_version` other than 1, a name mismatch, or binding params outside
   the declared schema. After import it checks `SPEC_VERSION` and a callable `open_array`, and
   `api.check_array` validates every opened array's axes, shape and access.
3. **Real-data test crops, published as a required commit status.** The mia-data worker runs two sandboxed
   cluster jobs against the exact PR head: `harness crop --n 4`, then `harness verify` and `harness preview`
   in a separate job that loads no snippet code, so a snippet cannot forge its own report. Verify checks every
   PNG decodes at the right size and bit depth, the box lies within the recorded shape, the content and
   overlap filters pass, labels are present, `crop.json` is complete and checksums are unique. The worker posts
   the crops on the PR and sets `mia-data/crops` on that SHA. After merge, a tree-hash comparison confirms
   the merged snippet is the one that was tested. Hand-written snippets go through the same path via
   `mia-data test-pr`.
4. **Harness unit tests** (`cd harness && pixi run test`) cover the harness and the snippets in this repo,
   loaded through the real loader over a local range server (`tests/test_snippet_*.py`). CI-checker tests:
   `cd harness && pixi run ci-test`.

Comments and labels never authorise a merge; only the two required statuses and a review do.

## Tensorizers

`tensorizers/<name>/` holds the second kind of pluggable unit: a pure function from a stack of co-centred,
multi-resolution patches to the arrays a model consumes (SPEC §8). The harness stages a dataset into an OME-Zarr
pyramid through its reader snippet (`harness stage`), reads a crop descriptor's patches back (`harness tensorize`)
and runs the tensorizer, checking its output against its own `describe()`. The same PR rules apply: one new
directory, immutable once merged, no I/O, static import check. `muvit-2d` is the first.

## Harness

`harness/` is a pip-installable package with a CLI (SPEC §7): `smoke` opens every array in a binding and
reads one patch, `crop` samples and writes crops, `verify` checks a crop directory without loading snippets,
`preview` draws label overlays. Humans own it; agents use it and never modify it. Jobs pin the harness
checkout's commit and `pixi.lock` digest when a round is admitted, and refuse to run if either moved.

## Branch protection

Set on `main` once the repo exists (not applied yet):
- require the status checks `check` and `mia-data/crops` (the latter restricted to the mia-data app, once it exists);
- require one approving review; dismiss stale approvals.

PRs that touch nothing under `snippets/` get `mia-data/crops` = success from the poller. Until the poller
runs, merge maintainer PRs by review alone, before protection is switched on. Changes to `.github/` itself
remain guarded only by the required review and autodev's refusal of agent commits that touch `.github/`.
