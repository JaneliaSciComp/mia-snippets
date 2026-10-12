# Writing a snippet (for autodev)

Read `SPEC.md` first, then `README.md`. The issue body carries the record, the match agent's report of what
fails, and a draft binding. Then:

1. Add exactly one new directory `snippets/<name>/` with `snippet.py`, `snippet.yaml`, `README.md` and
   `examples/<record-id>.yaml`. Never change anything else; CI rejects it.
2. `snippet.py`: `from mia_harness.api import IO, Array, Source`, `SPEC_VERSION = 1`, and a fully type-hinted
   `def open_array(src: Source, io: IO) -> Array`. All bytes come through `io`. Only `requires` from
   `harness/allowlist.txt`; if you need another dependency, say so in the issue and stop.
3. The example binding is the draft from the issue, corrected: every `snippet:` names your snippet, every
   `params` key is declared in your `snippet.yaml`, and `reasons` explains each non-default choice.
4. Test it before you finish. Save the issue's **Record** block as `record.yaml` in the workspace (do not
   commit it) and run:

   ```
   MIA_HARNESS_JOB_BYTES=5000000000 MIA_HARNESS_ALLOWED_HOSTS=<the URLs' hosts, comma-separated> \
     harness smoke examples/<record-id>.yaml --snippets . --record record.yaml --out smoke.json
   ```

   `harness` is on PATH from the pinned harness environment. The job's sandbox allows only the data hosts
   the policy grants; a `HostNotAllowed` means report the host in the issue, not work around it.
   Paste the smoke summary into the PR description.

The PR is tested on real data by the mia-data worker (`mia-data/crops` status on your head) and merged only
after a human review. A failing status comes back as a comment; fix and push a new head.
