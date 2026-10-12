# mia-snippets

Immutable "snippet" readers for microscopy data formats, plus a human-owned `harness` package
(`smoke`, `crop`, `verify`, `preview`) that samples 256×256 PNG crops through them. See `SPEC.md`.

Snippets are immutable once merged and are human-approved; a wrong one is withdrawn in `WITHDRAWN.yaml`, never edited.

Harness tests: `cd harness && pixi run test`.
