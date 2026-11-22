# ome-zarr2-zip-i303

Reads one resolution level of an NGFF (OME-Zarr v2) image or label whose Zarr store is a **directory inside a
zip**: `.zarray`, the group's `.zattrs` and every chunk are zip members. That is how a SpatialData object is
published as one file (`X.zarr.zip` holding `X.zarr/images/<name>/0`, `X.zarr/labels/<name>/0`, …). Written for
issue 303, where `zarr2-zip-jpegxr-cz-markers` (JPEG XR chunks only) and `zarr2-http` (plain objects, `.zattrs`
in the array directory) both refuse these arrays.

- **URL.** Exactly one: the level array's directory inside the zip, `<zip url>::<group dir>/<array dir>`
  (`…/X.zarr.zip::X.zarr/images/fluorescence/0`), or its `.zarray` member (`…/0/.zarray`). Both open the same
  array. **Prefer the `.zarray` form in a binding**: it is a member of the zip, the directory is not, and
  `harness fetch` fetches every binding URL. No globs.
- **Axes.** The array directory has no `.zattrs`. The group one directory up must have one, and its
  `multiscales` must list the array's directory name as a dataset `path`; that multiscale's `axes` (NGFF 0.4
  objects or 0.3 names) are the array's axes in storage order. They must be distinct letters of `c z y x` with
  `y` and `x` among them and one per array dimension, otherwise open raises. A `t` axis is refused.
- **Voxel size.** The level's `coordinateTransformations` `scale` (times the multiscale's own `scale`, if it has
  one), converted to nm by each spatial axis's `unit` (`angstrom`, `nanometer`, `micrometer`, `millimeter`,
  `centimeter`, `meter`). `None` when a scale is missing or not positive or a spatial axis has no unit from
  that list. **SpatialData writes no unit** (its axes are `{"name": "y", "type": "space"}` and its scale is 1),
  so its arrays report `None`: the file does not say, and the snippet does not take the record's word for it.
- **Other transformations are not read.** `translation`, and SpatialData's `identity` and `affine` to named
  coordinate systems, are ignored: the array is presented on its own pixel grid, in storage order. Two arrays
  of one store pair when their shapes match; whether they also share their transformations is the binder's
  check (see the example).
- **`.zarray`.** `zarr_format: 2`, a bool, integer or float `dtype` (any byte order; the array reports the
  native one), `order` `C` or `F`, no `filters`, `compressor` `null`, `blosc` (any inner codec and shuffle),
  `zlib`, `gzip` or `zstd`, `dimension_separator` `/` or `.` (the default). Anything else raises at open,
  naming what was found.
- **Access.** `plane` when one chunk spans the whole yx plane, else `chunk`. `open_array` reads the zip's
  directory (`io.list`, cached by the harness), `.zarray` and the group's `.zattrs`; no chunk. `read` gets the
  chunk members its region intersects, each whole: by range when the zip stores the member, staged through
  `io.fetch` when it is deflated (as in the example's zips, where even the blosc chunks are deflated). Up to
  128 MiB of decoded chunks are kept per array, and always the last one, however large.
- **Missing chunks.** A chunk the zip has no member for was never written and reads as `fill_value` (`null`
  reads as 0), as in Zarr. Whether a member exists is read from the zip's directory, so it costs no request.

It takes no params.

## Known limits

- **A channel axis is read with its chunks.** A `cyx` array chunked `(5, 1024, 1024)` holds all channels in
  each chunk, so one channel's patch fetches and decodes the others too (about 10 MB decoded per chunk).
- **A chunk is fetched and decoded whole**, so the cost of a patch is the store's chunking. Four of the
  example's raws are chunked up to 5×3663×3663 (134 MB decoded, about 78 MB in the zip): one 256×256 patch
  across four such chunks fetched 314 MB and took four minutes from `ftp.ebi.ac.uk`, with 400 MB of memory
  at the peak. A label in 8192×8192 uint16 chunks is 128 MiB decoded for any patch inside one. One such
  chunk is kept per array.
- **Only the url's member is in the binding's `urls`.** The group's `.zattrs` and the chunk members are read
  through `io` (counted, host-checked, the zip's validators recorded) but are not prefetched by
  `harness fetch`, so an offline re-stage from the cache cannot reach them.
- **Semantic ids are whatever the array holds.** Nothing in an NGFF label names its ids; the example's are
  UBERON term numbers, found in the store's cell table, not by this snippet.
- Zarr v2 only (no `zarr.json`, shards or consolidated metadata), no filters, no `lz4`/`bz2`/`lzma`
  compressors outside blosc, no `t` axis, no pyramid (one level per array), and the group must be the array's
  parent directory. SpatialData shapes (`shapes.parquet`) and tables are not read.
- `omero` channel names are not read; which channel is which is the binder's knowledge.

## Example

`examples/s-biad3270-fetal-digit-multiplexed-if-cell-tissue-seg.yaml`: the 18 SpatialData objects of
S-BIAD3270 on `ftp.ebi.ac.uk` (`zarr_obj_SD_NI_2026/*.zarr.zip`, 16.6 GB together; in the zip whose directory was looked through, every
file member is deflated). In
each, `images/fluorescence/0` (`cyx`, 5 channels DAPI_INIT, DAPI_FINAL, PI16, PRG4, VWF, uint16, blosc-lz4,
`/` keys; chunks 5×1024×1024 in 14 stores, up to 5×3663×3663 in four) is the raw and
`labels/tissue_regions_manual/0` (`yx`, uint16, blosc-lz4, chunks 8192×8192 or thereabouts) its semantic
label, on the same grid: 7200×18000 to 25400×17000 pixels. All 36 arrays open and every label has its raw's
yx shape.

What was checked on the data, beyond `harness smoke` and `harness crop --n 4` of the whole binding:

- **The label is read where the store's own table says it is.** `tables/table_intensities/obs` of each
  store lists its cells (55,037 to 109,776) with a centroid (`x`, `y`, in pixels of the image) and the tissue
  region they lie in (`tissue_region_label_id`). The label read through this snippet at
  `[round(y), round(x)]` equals that id for 99.79% to 99.93% of the cells, in all 18 stores. In
  `14w_15658_D_31_region_001_subregion_1` (58,001 cells, 99.85%) a transposed read agrees for 5%, one
  mirrored in y for 14% and in x for 28%.
- **The raw is read where the cells are.** In that store's 2048×2048 window with the most cells (4,375), the
  median of DAPI_FINAL at the centroids is 29,581 against 7,062 over the window, and 9,205 with the centroids
  mirrored in x (DAPI_INIT: 24,456 / 6,406 / 7,840). Drawn over DAPI there, the label's borders run between
  the tissues the nuclei show: sparse round nuclei (bone tissue), dense aligned ones (dense regular
  connective tissue), loose ones, and the epithelium at the edge.
- **Image and label share their transformations** in every one of the 18 stores (the same affine to the
  `rotated` coordinate system, identity to `global`), so pairing them pixel for pixel is what the store means.
- **The ids are UBERON term numbers**, by the same table's `tissue_region_uberon_term` and
  `tissue_region_uberon_label` columns: 2481 bone tissue, 7845 dense regular connective tissue, 11825 loose
  connective tissue and 19204 skin epithelium in every label, 2283 nail matrix in one, and 1 for `artefact`
  (`ARTEFACT:0000001`) in three. 0, 33% to 70% of each label, is where no region was drawn.

Other layouts (`.` keys, stored members, `F` order, big-endian and other dtypes, zlib, gzip, zstd,
blosc-zstd with bitshuffle, uncompressed, unwritten chunks, NGFF 0.3 axes, units, levels other than `0`) and
the errors were checked against arrays written by zarr-python, zipped, over a local range server.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it).
