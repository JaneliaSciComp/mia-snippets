# image-targz-glob

Reads the PNG and JPEG members of a gzip-compressed tar archive (`.tar.gz`) as one array: the members a glob
over their names matches, one per z plane. It is `image-tiles-glob` for a container that has no index. A member
with samples (RGB) is returned as a `c` axis, `zcyx`; the format is taken from each member's first bytes, not
its name.

- **URL.** Exactly one per array: the archive's plain URL. The members are named by a param, not by
  `<url>::<glob>`, because `io` reads `::` as a zip member.
- **`members`** (required): a glob over the full member names in the tar (`fnmatch` syntax, case-sensitive:
  `*`, `?`, `[...]`; `*` also crosses `/`), e.g. `01_source_imagery/non_aeg_raw_plag/Rumania/10/*.JPG`. A name
  without glob characters is one member, and the scan stops at it. Only regular files match.
- **Reaching the members.** A tar.gz has no index and cannot be read by range. `open_array` opens the archive
  with `io.open` and inflates the gzip stream from its first byte, once, to read the member names: that costs
  every compressed byte up to the last member wanted, and nothing behind it. The scan asks `io` for 1 MiB,
  then 2, 4, ... up to 32 MiB at a time (four range requests side by side), so the bytes are rounded up to the
  end of the read the last member's header fell in. The archive is never fetched whole, unless the server has
  no Range or Content-Length (`NeedsFetch`), where `io.fetch` stages it.
- **`count`** (optional): how many members the glob is to match. The scan stops at the `count`-th match in
  archive order; fewer matches raise. Without it the archive is inflated to its end, which for a multi-GB
  archive is all of its bytes. A match further into the archive than the `count`-th is not seen, so `count`
  is for a folder whose full number of files is known (tar stores a folder's files together); give that
  number and the reason in the binding.
- **Plane order and pairing.** The matched members are sorted by full name, whatever order the archive stores
  them in. **`key`** (optional) is a regular expression searched in each matched name: the planes are then
  sorted by its groups (the whole match when it has none). Use it when two arrays name the same tile
  differently, e.g. a raw `.../<sample>/<tile>.JPG` and a label `.../<tile>/clean_instance_labels_uint16.png`
  in another archive: `([^/]+)\.JPG$` and `([^/]+)/clean_instance_labels_uint16\.png$` give both the key
  `<tile>`, so z index k of the label is the map of z index k of the raw when both arrays hold the same set of
  tiles. A name `key` does not match, or two members with one key (or one name), raise. The snippet cannot
  compare the sets of two arrays, only their number (the harness checks the shapes), so the binding's
  `reasons` must say why the names pair and that the sets are the same.
- **Files.** PNG (8 or 16 bit, grey or colour) or JPEG. Every member of one array must give the same shape
  and dtype as the first; one that differs raises at `read`.
- **Axes.** A member without samples is `zyx`. A member with samples is `zcyx`, `c` in the file's sample order
  (R, G, B). With one member the `z` axis is dropped (`yx`, `cyx`).
- **Values** are the decoder's (`imagecodecs`), unchanged: a label PNG returns its stored ids. JPEG is lossy,
  so a JPEG is fit for raw images, not for ids.
- **Access.** `plane`: one member is one z plane, and any region of it costs the whole member. The scan keeps
  no pixels. For each matched member it keeps a copy of the inflater's state at the 1 MiB step of the archive
  the member starts in (about 39 kB per copy, shared by the members of one step). `read` re-enters the gzip
  stream there, so a plane costs its own member and at most 1 MiB of compressed bytes before it, served from
  `io`'s block cache: no network after the scan, except for the tail of the last member. `open_array` decodes
  the first plane only (shape and dtype). The last decoded member is kept, so the three `c` planes of one tile
  are decoded once.
- **dtype.** The file's. **Voxel size:** `None`; PNG and JPEG resolution fields describe print size.

## Known limits

- Bytes and time: the scan costs the archive's compressed bytes up to the last member wanted, on every job
  (the block cache is per job). Members deep in a multi-GB archive can pass the job's byte cap
  (`BudgetExceeded`); bind folders near the archive's start, with `count`.
- Each array scans the archive itself. Two arrays on one archive share the bytes through `io`'s cache but each
  inflates the stream again (about 5 s per GB of tar).
- A palette PNG or a 1/2/4-bit grey PNG raises: the decoder would turn palette indices into colours and
  rescale low bit depths to 0..255, so stored ids would be lost.
- Samples are exposed as stored: an RGBA member has 4 `c` planes. There is no param to pick or drop a sample.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour tile yields single-sample
  crops.
- Each member is read whole into memory, so this is for tiles, not for large images. Members of differing
  shape (e.g. crops of varying size) cannot be one array.
- Only `.tar.gz`; a plain `.tar`, `.tar.bz2` or `.tar.xz` is not read. One archive per array.
- Trailing bytes behind the last gzip stream that are not another gzip stream raise.

## Example

`examples/zenodo-22130319-plagioclase-cold-cl-ssl.yaml`: PlagNetPlus, cold-cathodoluminescence scan tiles of
volcanic rock thin sections (JPEG, RGB `uint8`, 2076x3088) in `01_source_imagery.tar.gz` (3.9 GB) and one
`uint16` instance map of plagioclase crystals per tile in `02_segmentation_resources.tar.gz` (1.1 GB). The
binding reads the two samples at the start of the raw archive, `Rumania/10` (144 tiles) and `Rumania/5` (174
tiles), as `zcyx` (144, 3, 2076, 3088) and (174, 3, 2076, 3088), with their label maps as `zyx`. Raw and label
are paired by tile name with `key`; the pairing was checked on the data (same 318 tile names on both sides,
label outlines on the crystals of the same-named tile). Smoke fetched 568,328,192 bytes: the first 447 MiB of
the raw archive and the first 95 MiB of the label archive. The example binding's `snippets_commit` is the
commit before this snippet's own (a binding can't name the commit that adds it); a binding written by the
worker names a commit that contains the snippet.
