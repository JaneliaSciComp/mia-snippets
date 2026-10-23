# mia-snippets spec (version 1)

## 1 Layout

```
mia-snippets/
├── SPEC.md                    this section, the contract (human-owned)
├── WITHDRAWN.yaml             snippets found to be wrong (human-owned)
├── harness/                   executable side of the spec (human-owned)
├── ci/                        PR rules (human-owned, §5)
├── .github/workflows/check.yml
├── snippets/<name>/           one directory per snippet, immutable once merged
│   ├── snippet.yaml           metadata (§3)
│   ├── snippet.py             the reader (§2)
│   ├── README.md              what it reads, how params are chosen, known limits
│   └── examples/<record-id>.yaml   ≥ 1 binding it was tested on (§4)
└── tensorizers/<name>/        one directory per tensorizer, immutable once merged (§8)
    ├── tensorizer.yaml        metadata: name, spec_version, summary, params, requires
    ├── tensorizer.py          describe() and tensorize()
    └── README.md              the sample it produces and its limits
```

`<name>` is a lowercase slug that is never reused, e.g. `tiff-zip-member`, `mrc-http`, `tiff-slices-zip`,
`hdf5-dataset`. A changed snippet is a new directory with a new name, such as `tiff-zip-member-2`, and its
`snippet.yaml` sets `supersedes: tiff-zip-member`. Bindings keep pointing at the version they were tested with.

## 2 Interface

```python
# snippets/<name>/snippet.py
from mia_harness.api import IO, Array, Source   # the only harness module a snippet may import

SPEC_VERSION = 1

def open_array(src: Source, io: IO) -> Array:
    """Open one array (raw or label) described by `src`. Must not read voxel data yet."""

class Array(Protocol):                     # defined in harness/src/mia_harness/api.py
    axes: str                              # e.g. "zyx", "czyx", "yx": letters from "czyx", one per dimension
    shape: tuple[int, ...]
    dtype: numpy.dtype
    voxel_size_nm: dict[str, float] | None # per spatial axis, if the file says so, else None
    access: Literal["chunk", "plane", "file"]   # cheapest read unit for THIS file; may differ per file
    def read(self, region: dict[str, int | slice]) -> numpy.ndarray   # e.g. {"c": 0, "z": 17, "y": slice(…), "x": slice(…)}
```

- **Type hints are required.** `open_array` is annotated exactly as above, and CI rejects it otherwise. Use
  hints throughout the snippet; `Source`, `IO`, `Array` and the exceptions below all come from `mia_harness.api`.
- **`Source`** holds:
  - `urls: list[str]`: one per file, sorted. There are several when the dataset is one file per Z slice.
    Zip members are written `<zip url>::<member>`.
  - `params: dict`: snippet-specific parameters declared in `snippet.yaml`, e.g. an HDF5 key or an axis
    override.
- **`access` is reported at runtime** by the opened `Array`, because the same snippet can meet a stored zip
  member in one dataset and a deflated one in the next. `snippet.yaml` doesn't declare it.
- **All I/O goes through `io`**, which counts network bytes and enforces the job's byte cap:
  - `io.open(url)`: a seekable binary file backed by HTTP or S3 range requests and an on-disk cache
  - `io.fetch(url)`: a local `Path` holding the whole file, staged once
  - `io.list(url)`: lists zip members

  `io` records each file's validators (ETag, Last-Modified, size) for provenance, and refuses a server that
  answers a range request with the whole body.
- **Exceptions are part of the API.** `harness.api` defines `NeedsFetch` (raised by `io.open` for a file that
  can't be read by range, e.g. a deflated zip member: catch it and use `io.fetch`), `HostNotAllowed` and
  `BudgetExceeded`. Snippets import them from `mia_harness.api`, the only harness module a snippet may import.
- **`io` follows redirects itself** and checks every hop's host against the hosts the job was granted
  (`MIA_HARNESS_ALLOWED_HOSTS`, set by the worker) before requesting it. A hop outside that set raises
  `HostNotAllowed`, with the host and the redirect chain so far, instead of failing later as a generic proxy or
  connection error. `ftp://` URLs raise the same error. The harness reports it as `host_not_allowed` in the
  command's JSON output, and the match agent's prompt says to report `blocked` with that host rather than
  work around it.
- Snippets never open sockets, call `requests`/`urllib`/`fsspec`
  directly, run subprocesses or write outside `io`'s staging.
- **Pure readers.** A snippet reads; it never crops, samples, resamples, normalises or writes output. Those belong
  to the harness, so every crop goes through the same code.
- **Independent.** A snippet imports only the harness API, its own module and the dependencies on the allowlist
  (§3). It never imports another snippet. Shared code goes into a new snippet, not into a common module.

## 3 `snippet.yaml`

```yaml
name: tiff-zip-member
spec_version: 1
summary: One TIFF from a zip member or a plain URL; deflated members are fetched whole.
formats: [tiff, ome-tiff]          # mia-agentic-search data.formats values it handles
containers: [zip, http]            # zip | http | s3 | local
params:                            # every param the binding may set; nothing else is accepted
  axes: {type: str, required: false, doc: "override when the TIFF's own axes are wrong; reason required in binding"}
requires: [numpy, tifffile, imagecodecs]   # must be in the harness environment's allowlist
tested_on: [bbbc034-hipsc-nuclei-3d]       # = examples/*.yaml
supersedes: null
```

A snippet that needs a dependency outside the allowlist can't add it. The agent reports this, and a human
extends the harness environment.

## 4 Binding

A binding says how to read one dataset. It is data, not code. The worker writes it once per stage into
`paths.state_dir`, and the workflow holds its path and hash. The status comment shows a copy. A new
snippet's PR includes the binding as `examples/<record-id>.yaml`, so its reviewer reviews both.

```yaml
record: bbbc034-hipsc-nuclei-3d
record_commit: 37e18f6e…
snippets_commit: 4c1d…              # mia-snippets commit the snippets below were taken from
raw:
  - snippet: tiff-zip-member
    urls: [https://data.broadinstitute.org/bbbc/BBBC034/BBBC034_v1_dataset.zip::AICS_12_134_C=2.tif]
    params: {}
labels:
  - snippet: tiff-zip-member
    urls: [https://data.broadinstitute.org/bbbc/BBBC034/BBBC034_v1_DatasetGroundTruth.zip::ground_truth_segmented.tif]
    params: {}
    pairs_with: 0                  # index into raw; shape must match on every axis except c
    label_key: manual_gt-nucleus-instance   # miao#13 label name, from the record (mia-agentic-search's rules)
    segmentation_type: instance    # instance | semantic | binary (decides PNG encoding)
reasons: ["axes zyx from ImageJ metadata, matches record", …]   # one per non-default decision
```

`raw` may list several volumes (e.g. one per file set). Crops are drawn across them.

## 5 Immutability and review (CI in `mia-snippets`)

`check.yml` runs on every PR, and branch protection requires it:
- **Only additions.** A PR may only add files under one new `snippets/<name>/` or one new `tensorizers/<name>/`. It fails if it changes or
  deletes anything under an existing snippet, or touches `harness/`, `ci/`, `SPEC.md`, `WITHDRAWN.yaml` or
  `.github/`, unless the PR author is a listed human maintainer. autodev already refuses agent commits that
  touch `.github/`.
- **Valid metadata.** `snippet.yaml` validates against the harness schema, `name` matches the directory and
  isn't already used, every `requires` is on the allowlist, and every `tested_on` has an example binding.
- **Static import check.** It rejects network, subprocess and `os.system` imports, `__import__`, `exec`, `eval`
  and `compile`, the interpreter modules `sys`, `builtins`, `runpy`, `code` and `pty`, imports of other snippets,
  and `pickle` or any `allow_pickle` that is not a false constant. This supplements the sandbox; it doesn't
  replace it.
- **Real-data tests run on the cluster, not on the CI runner.** The mia-data worker publishes a
  `mia-data/crops` **commit status** on the exact PR head SHA, alongside a readable PR comment.
  - Branch protection requires both `check` and `mia-data/crops`, with the status restricted to the trusted
    app that publishes it.
  - Comments and labels never authorise a merge.
- **PRs that add no snippet** (harness, `ci/`, `SPEC.md` or `WITHDRAWN.yaml` changes by a maintainer) get
  `mia-data/crops` = success, "no snippet in this PR", from the poller. The status is set when the head's
  diff against `main` touches nothing under `snippets/`.
- **Human-authored snippet PRs** (the first hand-written snippets) get their status from the manual
  admission command `mia-data test-pr`.

## 6 Withdrawing a snippet

`WITHDRAWN.yaml` lists snippets that turned out to be wrong, e.g. one with a wrong axis order:

```yaml
- {name: tiff-zip-member, reason: "transposes yx on BigTIFF", at: 2026-11-02, by: <maintainer>}
```

Withdrawing is human-only (§5) and never deletes the snippet. Its effects:
- The match agent skips withdrawn and superseded snippets.
- The harness refuses to run a withdrawn snippet, so pending tests and production for it stop.
- **Published crops are never deleted by the system.** When `manifest.json` is rebuilt (on every publication,
  or with `mia-data reindex`), every crop whose `crop.json` names a withdrawn snippet is marked
  `withdrawn: true` with the reason. Consumers must skip those crops.
- `mia-data crops-by-snippet <name>` lists them. Deleting files is a manual operator action outside the
  system.
## 7. Harness CLI

```
harness smoke   <binding.yaml> --snippets <dir> --record <record.yaml>   # open every array, check vs record,
                                                                       # read one 256×256 patch per raw (+ labels)
harness crop    <binding.yaml> --snippets <dir> --record <record.yaml> --n N --seed S --round R --batch B
                --out DIR [--existing MANIFEST]                        # sample + write crops
harness verify  DIR --binding <binding.yaml> [--existing MANIFEST]     # output checks; loads no snippet code
harness preview DIR --out PREVIEW                                      # label overlay PNG per crop
harness stage   <binding.yaml> --snippets <dir> --record <record.yaml> --levels 1,2,4 --patch 256 --out DIR
                [--voxel-size-nm z,y,x]                                # one verified NGFF pyramid per raw (buffers);
                                                                       # stage.json checks cover raw and every label layer
```

`--snippets` is a read-only snapshot of one exact commit, never a branch resolved again. Snippets are imported
from that directory only. Every command takes its byte cap from the job environment; command-line options can't
raise it.

## 8 Tensorizers

A tensorizer turns a stack of co-centred patches, one per pyramid level, into the arrays a model consumes. It is
the second pluggable unit, reviewed and frozen like a snippet, and composed with readers only by the harness
(`harness stage` writes pyramids through snippets; `harness tensorize` reads them and calls the tensorizer).

```python
# tensorizers/<name>/tensorizer.py
from mia_harness.tensor_api import Sample, Stack, TensorSpec   # the only harness module a tensorizer may import

SPEC_VERSION = 1

def describe(params: dict) -> TensorSpec:
    """Output arrays by key: (shape with the symbols L, C, P, dtype). A key ending "/*" matches any number of keys."""

def tensorize(stack: Stack, params: dict) -> Sample:
    """Pure: no I/O. Returns the described arrays plus a JSON-safe "meta" dict."""
```

`Stack.levels` are `LevelPatch(factor, img (C, P, P), labels {key: (P, P)}, scale_nm, translation_nm, start_px)`.
The harness checks every output against `describe()` (shape, dtype, no undescribed keys) and fails the command on
a mismatch. The first tensorizer is `muvit-2d`.
