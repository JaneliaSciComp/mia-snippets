# ome-zarr2-http-timepoint

Reads one resolution level of an OME-Zarr image stored as Zarr v2 objects under an HTTP(S) URL (public S3
included), **at one timepoint**. An `Array` has no time axis, so a `tczyx` level array is presented as `czyx`
with `params.t` fixed on `t`. Axis order and voxel size come from the image group's `multiscales` metadata.
Like `zarr2-http` it never lists the store: chunk keys are computed from `.zarray`.

- **URL.** Exactly one: a level's array directory (`…/<image>.ome.zarr/0`, trailing slash or not) or its
  `.zarray` object (`…/<image>.ome.zarr/0/.zarray`). **Prefer the `.zarray` form in a binding:** `harness
  fetch` and the prefetch of `harness stage` fetch every binding URL whole, and the directory URL is not an
  object (404). The URL names the level, so any level can be bound; `0` is full resolution. The image group
  URL itself (`…/<image>.ome.zarr`) is not accepted.
- **Group metadata.** The image group is the directory one up from the array, and its `.zattrs` is read once
  at open. In `multiscales`, the first entry with a dataset whose `path` is the array's directory name gives:
  - **axes**: OME-NGFF 0.4 (`[{name, type, unit}, …]`) or 0.3 (`["t", "c", "z", "y", "x"]`), names from
    `t c z y x` (case-insensitive), distinct, one per array dimension, with `y` and `x`. Anything else raises.
    With no `axes` (OME-NGFF 0.1 / 0.2, or no `.zattrs` at all) a rank-5 array is read as `tczyx`, which those
    versions require; any other rank raises.
  - **voxel size**: the dataset's `coordinateTransformations` `scale` (times the multiscale-level `scale`, if
    there is one), converted to nm by each space axis's `unit` (`picometer`, `angstrom`, `nanometer`,
    `micrometer`, `millimeter`, `centimeter`, `decimeter`, `meter`). `voxel_size_nm` is `None` when there is
    no scale, a space axis has no unit or another unit, or a value is not positive (so always for 0.1–0.3,
    which carry no unit). `translation` is not read.
- **Time axis.** Not presented. `params.t` is the index read on it; see Params.
- **Channel axis.** Presented as `c` when it has more than one entry. A channel axis with **one entry is not
  presented** (`tczyx` (1, 1, 30, 1248, 1848) opens as `zyx`). The harness reads a label that has a `c` axis
  at the channel index of the raw crop, so a one-channel mask kept as `czyx` could only be read for crops of
  raw channel 0; without the axis it pairs with every channel of its raw.
- **Array metadata.** `.zarray` must say `zarr_format: 2`, a bool, integer or float `dtype` (any byte order;
  the array reports the native one), `order` `C` or `F`, no `filters`, and a `compressor` that is `null`,
  `zlib`, `gzip`, `blosc` or `zstd`. `dimension_separator` `/` and `.` (the default) are both read.
- **Access.** Always `chunk`. `open_array` reads the array's `.zarray` and the group's `.zattrs` only. `read`
  fetches the chunk objects its region intersects, each whole, through `io.open` (`io.fetch` when the server
  gives no Content-Length or ignores Range), and decodes them. With the usual one-plane chunks
  (`1, 1, 1, Y, X`) a 256×256 patch costs one chunk object. Up to 128 MiB of decoded chunks are kept per array.
- **Missing chunks.** A chunk object the server answers **404** for was never written and reads as
  `fill_value` (`null` reads as 0), as in Zarr. Only 404 means that; any other status raises.

## Params

- `t` (optional int): 0-based index on the time axis.
  - Time axis longer than 1: **required**. Without it open raises, naming the valid range, rather than pick a
    timepoint silently. Out of range raises.
  - Time axis of length 1 (a fixed sample stored as `tczyx`): may be left out; 0 is the only timepoint.
  - No time axis: must be left out.

  A label gets the same `t` as the raw it pairs with; the snippet opens one array and cannot check that.
  Give the reason for the chosen timepoint in the binding.

## Known limits

- **One timepoint per bound array.** To crop several timepoints of a timelapse, list the store several times
  in the binding's `raw` (and its label alongside), each with its own `t`.
- **Raw and label timepoints are matched by index only.** For this record's H2B nuclear segmentations the
  label has 120 timepoints against 121 in the raw, and nothing in the stores says which raw timepoint is
  missing; do not bind those with this snippet until that is known.
- **Channels are not named.** `omero.channels` labels are not read. In this record the content of a channel
  index differs per image (the manifest lists it), and crops are drawn from every channel of a raw.
- The group must be the array's parent directory (dataset `path` of one component, as written by
  bioformats2raw, ome-zarr-py and bioio); an array under a nested dataset path is read as if it had no group metadata. Only the
  `multiscales` entry that lists the array is used; `labels`, `plate`, `well` and `bioformats2raw.layout`
  groups are not walked.
- **Only `.zarray` is in the binding's `urls`.** The group's `.zattrs` and the chunk objects are read through
  `io` (counted, host-checked, validators recorded) but are not prefetched, so an offline re-stage from the
  cache cannot reach them.
- A server that answers 403 instead of 404 for a missing key makes an unwritten chunk an error.
- Each chunk object costs a HEAD and a GET, and is read whole even for one row.
- Zarr v2 only: no `zarr.json` (OME-Zarr 0.5 / Zarr v3), no shards, no filters, no structured, string or object
  dtypes, no `lz4`/`bz2`/`lzma` compressors (blosc with any inner codec is read).

## Example

`examples/allen-emt-timelapse-hips-colonies.yaml`: the Allen EMT dataset's fixed image `3500007015_10` on
`allencell.s3.amazonaws.com`, raw `tczyx` (1, 4, 30, 1248, 1848) uint16 presented as `czyx`, and its
all-cells mask (1, 1, 30, 1248, 1848) uint8 {0, 255} presented as `zyx`; OME-NGFF 0.4, blosc-lz4 one-plane
chunks, `/` separator; voxel size z 2880, y 270.83, x 270.83 nm from the multiscales scale. No `t`: both
have one timepoint.

Tested on that data with `harness smoke` and `harness crop --n 4` (crops from raw channels 0, 1 and 3, mask
outlines following the cells in the bright-field channel). Outside the binding, the live timelapse
`3500006256_11` (raw (121, 3, 30, 1248, 1848), all-cells mask (121, 1, 30, 1248, 1848)) passes smoke and
`crop --n 4` with `t: 60` on both, and without `t` open raises "has 121 timepoints … set params.t (0..120…)".
Checked against arrays written by zarr-python over a local range server: every `t` of a `tczyx` array against
the source data, `blosc`/`zlib`/`zstd`/`gzip`/uncompressed chunks, both separators, a missing chunk,
big-endian data, stepped and negative slices, 0.3 axes, arrays without a time axis or without group metadata,
units and the multiscale-level scale, and that open requests only `.zarray` and `.zattrs`.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
