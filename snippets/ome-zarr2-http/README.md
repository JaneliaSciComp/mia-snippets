# ome-zarr2-http

Reads one resolution level of an OME-Zarr v2 image stored as plain objects under an HTTP(S) URL (public `s3://`
included), **one timepoint at a time**. The harness knows the axes `c z y x` only, so an image with a `t` axis
is opened once per timepoint (`params.t`); `c` and `z` can be fixed the same way, and `z_project: max` turns a
z stack into its maximum intensity projection so that a 2D label made on that projection pairs with its 3D raw.
Axes and voxel size come from the NGFF `multiscales` of the image group. It never lists the store.

- **URL.** Exactly one: a level array's directory (`…/X.ome.zarr/0`) or its `.zarray` object
  (`…/X.ome.zarr/0/.zarray`). **Prefer the `.zarray` form in a binding**: it is an object with validators,
  the directory URL is not. The group must be the directory directly above the array (`…/X.ome.zarr`), and
  its `.zattrs` `multiscales` must list the array's directory name as a dataset `path`; otherwise open raises.
  Level `0` is full resolution; another level is another array with its own shape and scale.
- **Axes.** From the multiscale's `axes` (NGFF 0.4 objects or 0.3 names), in storage order; names must be
  distinct letters of `t c z y x` and include `y` and `x`. A multiscale without `axes` (NGFF 0.1/0.2) is read
  as `tczyx` when the array has rank 5. The array the harness sees has the stored axes minus the ones the
  params take away.
- **Voxel size.** The level's `coordinateTransformations` `scale` (times the multiscale's own `scale`, if it
  has one), converted to nm by each spatial axis's `unit` (`angstrom`, `nanometer`, `micrometer`,
  `millimeter`, `centimeter`, `meter`). Reported for the spatial axes the array still has; `None` when a
  scale is missing or not positive or a spatial axis has no unit from that list. Translations are ignored.
- **`.zarray`.** `zarr_format: 2`, a bool, integer or float `dtype` (any byte order; the array reports the
  native one), `order` `C` or `F`, no `filters`, `compressor` `null`, `blosc` (any inner codec and shuffle),
  `zlib`, `gzip` or `zstd`, `dimension_separator` `/` or `.`. Anything else raises at open.
- **Access.** `plane` when one chunk spans the whole yx plane (the usual OME-Zarr time-lapse layout, and the
  cheapest unit is then the plane), else `chunk`. `open_array` reads `.zarray` and the group's `.zattrs`
  only. `read` fetches the chunk objects under its region, each whole, and keeps up to 256 MiB of decoded
  chunks per array. A chunk the server answers **404** for was never written and reads as `fill_value`.

## Params

All optional in the schema; give a reason in the binding for each one set.

- `t`: 0-based timepoint. **Required when the store has more than one timepoint**; a `t` axis of length 1 is
  dropped without it; an error when the store has no `t` axis. A binding lists one raw entry per timepoint
  it wants, each with its labels at the same `t`.
- `c`: 0-based channel; the array then has no `c` axis. Use it for a label's single channel (the harness
  reads a label at the raw's channel index when the label has a `c` axis, so a one-channel label under a
  multi-channel raw must not keep it) and for a raw whose label belongs to one channel.
- `z`: 0-based z plane; the array then has no `z` axis. For a label's single plane, or the one raw plane a
  2D label belongs to **when the dataset says which**.
- `z_project: max`: the array has no `z` axis and each pixel is the maximum over all z planes, in the stored
  dtype. Exclusive with `z`. Only for a raw whose label was made on the maximum intensity projection,
  **with the dataset's own statement of that as the reason**; it is not a way to make shapes match.

Indices are checked against the axis length at open. Without `c`, `z` or `z_project` those axes stay and the
harness samples over them.

## Known limits

- **One timepoint per array.** Crops are drawn only from the timepoints a binding lists; nothing samples
  over time.
- **A projection costs every z plane.** With `z_project`, a read fetches the chunks of all z planes under
  its box: 25 planes of 1712×1744 uint16 are about 75 MB over the network for the first patch of a
  timepoint (then cached). The projection is computed in the reader because the label exists only on it;
  it is a maximum over stored values, with no rescaling.
- **Only `.zarray` is in the binding's `urls`.** The group's `.zattrs` and the chunk objects are read
  through `io` (counted, host-checked, validators recorded) but not prefetched by `harness fetch`.
- A server that answers 403 instead of 404 for a missing key makes an unwritten chunk an error.
- Each chunk object is read whole (a HEAD and a GET), even for one row.
- Zarr v2 only (no `zarr.json`, shards or consolidated metadata), no filters, no `lz4`/`bz2`/`lzma`
  compressors outside blosc, no pyramid (one level per array), no `labels/` group discovery, no plates or
  wells, and the group must be the array's parent directory.
- `omero` channel names are not read; which channel is which is the binder's knowledge.

## Example

`examples/allen-endothelial-shear-stress-ve-cadherin-timelapse.yaml`: position `20250319_…_P0` of the Allen
endothelial shear-stress time-lapse on `allencell.s3.amazonaws.com`. Raw `tczyx` 543×2×25×1712×1744 uint16
(blosc-lz4, one yx plane per chunk) is bound at `t: 0, c: 0, z_project: max`; the VE-cadherin cell
segmentation and the label-free nuclear segmentation (543×1×1×1712×1744 uint16, blosc-zstd bitshuffle, two
timepoints per chunk) at `t: 0, c: 0, z: 0`. All three open as `yx` 1712×1744 at 382.016 nm.

Why the projection: the dataset's `cdh5_seg_validations_zarr/<same name>` store names its channels
`VE-cadherin_mEGFP_maximum_intensity_projection`, …, `VE-cadherin_mEGFP_segmentation_split_by_nuclei`, ….
Read through this snippet, its t 0 channel 0 equals the maximum over the 25 z planes of raw t 0 channel 0 in
every pixel, and its channel 6 equals the cell segmentation at t 0 in every pixel. The cell borders of the
label lie on bright VE-cadherin in the projection (border / interior mean intensity 1.21, against 1.00 with
the borders shifted by 40 px); the best single plane (z 14) reaches 1.21 too but no plane is the label's.

Tested on that data with `harness smoke` and `harness crop --n 4`. Other chunkings (chunks cutting t, z, y
and x), `.` keys, `F` order, big-endian, zlib, uncompressed, missing chunks, NGFF 0.3 and pre-0.3 axes,
units and the param errors were checked against arrays written by zarr-python over a local range server.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it).
