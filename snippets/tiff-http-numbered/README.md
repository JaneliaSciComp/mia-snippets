# tiff-http-numbered

Reads a z stack stored as one single-page grey TIFF per slice under consecutively numbered plain URLs
(`frames/0000.tif` … `frames/0720.tif`) and presents it as one `zyx` array. What it adds over
`tiff-zip-glob-rgb`, which also stacks plain URLs: the binding does not list every file. A directory served
over HTTP cannot be globbed (`io.list` lists zip members only), and a binding with one URL per slice of a
721-slice raw and its 721 masks was too large for the match agent to draft.

- **URLs and `last`.** With `params.last`, the binding gives exactly one URL, the first file. The counter is
  the last run of digits in its file name (`0000` in `0000.tif`, also in `0000_mask.tif`); the snippet counts
  it up by 1 to `last` inclusive, keeping the zero padding, and stacks the files in that order. `last` and the
  first number come from the record's listing (`path_pattern` and its number range, `listing_complete`). The
  URL must be a plain file URL: no zip member, query or fragment. `last` below the first number, or too long
  for a zero-padded counter, raises at open.
- **Without `last`** the URLs are stacked as given (binding order); one URL gives a `yx` array.
- **Files.** Each file is a single-series, single-page `YX` TIFF, any compression `tifffile`/`imagecodecs`
  decodes, all with the same shape and dtype. Multi-page stacks belong to `tiff-zip-member`, RGB tiles to
  `tiff-zip-glob-rgb`; both raise here.
- **Pairing.** A raw and a label bound with the same first number and `last` pair plane for plane when the
  two series share their numbering. The snippet cannot check that across arrays, so the binding's `reasons`
  must say why the numbers pair.
- **Access.** `plane`: one file is one plane, read through `io.open` (or staged with `io.fetch` when the
  server ignores Range). `open_array` reads only the first file's header (shape, dtype); the other files are
  requested when `read` asks for their plane, and a file that is missing or differs from the first raises
  then, not at open.
- **dtype.** The file's. **Voxel size:** `None`; plain TIFF resolution tags describe print size, not pixels,
  and the z spacing is in no file.

## Known limits

- The files between the first and `last` are not checked to exist at open: a gap in the numbering is an HTTP
  error at the first read of that plane. Step is always 1.
- Only the first file is a binding URL, so the harness records validators for it alone, and `harness stage`
  prefetches it alone; the other planes are fetched when the stage reads them, which a re-stage from the cache
  with no network cannot do.
- The harness does not retry: `ftp.ebi.ac.uk` closed one connection mid-body and stalled another while this
  snippet was written; a repeat run passed.
- No sample (RGB) axis, pyramid levels or voxel size.

## Example

`examples/empiar-12581-mouse-brain-mitochondria-fib-sem.yaml`: EMPIAR-12581, 721 FIB-SEM slices
(1768x2048 uint8, uncompressed, 3.6 MB each) and 721 LZW mitochondria masks (`{0, 255}`) with the same
numbers, bound as one `721x1768x2048` raw and its binary label. The sparse 1-bit PNG labels (every 100th
slice) and the RGB overlay renderings are not bound. The example binding's `snippets_commit` is the commit
before this snippet's own (a binding can't name the commit that adds it); a binding written by the worker
names a commit that contains the snippet.
