# zarr2-http

Reads one Zarr v2 array stored as plain objects under an HTTP(S) URL (public `s3://` included): `.zarray`,
`.zattrs` and one object per chunk. It never lists the store. Chunk keys are computed from `.zarray`, so it
works on a bucket that has no directory index, where the array URL itself and every `<dir>/` URL answer 404.
What it adds besides the format is `params.grid`: an array that is an offset crop of another (a label block
inside its raw) is presented on that other array's voxel grid, so the two pair shape-for-shape.

- **URL.** Exactly one, either the array directory (`…/1E.zarr/volumes/raw`, trailing slash or not) or its
  `.zarray` object (`…/1E.zarr/volumes/raw/.zarray`). Both open the same array. **Prefer the `.zarray` form
  in a binding:** `harness fetch` and the prefetch of `harness stage` fetch every binding URL whole, and the
  directory URL is not an object (404), while `.zarray` is one with validators. The URL must be an array,
  not a group: for `<cell>.zarr/volumes/raw` bind the full path down to the array.
- **Metadata.** `.zarray` must say `zarr_format: 2`, a bool, integer or float `dtype` (any byte order; the
  array reports the native one), `order` `C` or `F`, no `filters`, and a `compressor` that is `null`,
  `zlib`, `gzip`, `blosc` or `zstd`. `dimension_separator` `.` (the default) and `/` are both read. Anything
  else raises at open, naming what was found.
- **Access.** Always `chunk`. `open_array` reads `.zarray` and `.zattrs` only (and the grid array's two, with
  `grid`). `read` fetches the chunk objects its region intersects, each whole, through `io.open` (`io.fetch`
  when the server gives no Content-Length or ignores Range), and decodes them. A 256×256 patch of one plane
  of a 128³-chunked array costs at most 9 chunk objects. Up to 128 MiB of decoded chunks are kept per array,
  so neighbouring planes of the same chunks are not fetched or decoded again.
- **Missing chunks.** A chunk object the server answers **404** for is a chunk that was never written and
  reads as `fill_value` (`null` reads as 0), as in Zarr. Only 404 means that; any other status raises.
- **Axes.** By rank, in storage order: 2 → `yx`, 3 → `zyx`, 4 → `czyx`. Other ranks or orders need
  `params.axes`.
- **Voxel size.** `.zattrs` `resolution`, a list with one number per array dimension in axis order, read as
  nanometres (the funlib / daisy / gunpowder convention the ASEM stores use, where `offset` and `resolution`
  are world units in nm). The spatial axes' entries are reported; `None` when the attribute is absent, has
  another length, or holds a non-positive or non-numeric value. No other convention (OME-NGFF `multiscales`,
  `pixelResolution`, `scale`) is read.

## Params

- `grid` (optional): URL of another Zarr v2 array, in either URL form; normally the raw a label pairs with.
  The opened array then has the **grid array's shape**. Its voxel 0 sits at index
  `(offset − grid offset) / resolution` per axis, from both arrays' `.zattrs`; inside its own extent it
  returns its stored voxels, everywhere else its `fill_value`, and a part that sticks out of the grid is not
  reachable. Both arrays need `offset` and `resolution` (one number per dimension), the resolutions must be
  equal and the shift a whole number of voxels; otherwise open raises. A read wholly outside the array's
  extent costs no request. Use it for a label that is a crop of its raw, and also for a label that already
  has the raw's shape, so that the offsets are checked instead of assumed. Give the reason in the binding.
- `axes` (optional): one `czyx` letter per array dimension, in storage order, when the default by rank is
  wrong. Give the reason in the binding.

## Known limits

- **Outside a placed crop, "not annotated" reads as background.** With `grid`, a label that covers only a
  block of its raw returns `fill_value` (0) for the rest of the raw grid, and a harness crop drawn there gets
  an empty label although the structure may be present in the raw. Nothing in the array tells the two
  apart. For ASEM's 2E `labels/mito` (200³ inside 2008×982×1890) almost every random crop falls outside the
  block. Whoever binds such a label should know that the label is only valid inside its block; restricting
  crops to it is the harness's or the plan's job, not this reader's.
- **Only `.zarray` (or the directory URL) is in the binding's `urls`.** As with `tiff-series-http`, the
  harness knows sources from `urls`: `crop.json` validators and `harness stage`'s `derived_from` cover the
  `.zarray` object, while `.zattrs`, the chunk objects and the `grid` array are read through `io` (counted,
  host-checked, validators in `io.validators`) but are not prefetched, so an offline re-stage from the cache
  cannot reach them.
- A server that answers 403 instead of 404 for a missing key (an S3 bucket without public `ListBucket`)
  makes an unwritten chunk an error, not `fill_value`.
- Each chunk object costs a HEAD and a GET, and is read whole even for one row.
- Zarr v2 only: no `zarr.json` (v3), no shards, no groups or consolidated metadata, no filters, no
  structured, string or object dtypes, no `lz4`/`bz2`/`lzma` compressors.
- No pyramid: one array is one resolution level.

## Example

`examples/aws-asem-fib-sem-organelle-ground-truth.yaml`: ASEM cell 1E on `asem-project.s3.amazonaws.com`,
raw `zyx` 751×1088×2817 uint8 and the clathrin-coated-pit label on the same grid (uint8 0/255), both zlib
chunks of 128³, 4 nm isotropic from `.zattrs`. The label is bound with `grid` = the raw; the shift is 0.

Tested on that data with `harness smoke` and `harness crop --n 4`, and outside the binding on cell 2E:
`labels/mito` (200³, `.zattrs` offset `[4060, 1264, 1176]` nm) opens with `grid` = 2E raw as 2008×982×1890,
its voxels are found at index (1015, 316, 294) with 31 % foreground as the record says, and its outline
follows the mitochondrion in the raw there. `zlib` is the only compressor met on real data; `gzip`, `blosc`,
`zstd`, uncompressed chunks, `/` separators, `F` order, big-endian and multi-byte dtypes, missing chunks and
clipped placements were checked against arrays written by zarr-python over a local range server.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
