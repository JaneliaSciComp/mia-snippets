# npy-local (test only)

Harness test fixture, never a real snippet: with `params.path` it reads a local `.npy` with
`numpy.load(path, mmap_mode="r")`, bypassing `io` (which only speaks http(s)); otherwise it loads `urls[0]`
through `io.open`, reading the whole file at open time. Axes come from `params.axes`, else `zyx`/`yx`.
