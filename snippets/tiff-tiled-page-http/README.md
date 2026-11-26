# tiff-tiled-page-http

Reads page 0, the full-resolution image, of one **tiled** TIFF with `tifffile`, tile by tile, from a plain URL
or a zip member (`<zip url>::<member>`). It is meant for whole-slide images: one JPEG-compressed page of
10^9..10^10 pixels in 256- or 512-pixel tiles, followed by reduced-resolution copies of it. Exactly one file
per array.

Why it exists: every other TIFF snippet decodes a page whole (`tif.asarray(key=page)`) and then slices it.
For `ASSIST_HCUV_0_A.tif` of `s-biad3206-assist-spindle-cell-wsi` (100800x88016x3, 883 MB of JPEG, 26.6 GB
decoded) that means the whole file for any crop. Here a region costs the tiles it touches: a 256x256 patch is
one to four tiles.

- **Open.** `tifffile.TiffFile(io.open(url))` and its first page: the TIFF header, the first IFD and its
  TileOffsets and TileByteCounts tables (and JPEGTables). No tile is fetched, and the other pages (the pyramid
  levels, a label or macro image) are never visited, so opening does not walk the IFD chain. On the example's
  slides that is 2-3 MiB in 1 MiB cache blocks.
- **Read.** The bounding box of the requested `y`/`x` selection is assembled from the tiles that overlap it,
  each fetched with one seek and read (in file order) and decoded by `TiffPage.decode`, which is what
  `tifffile` itself uses: JPEGTables are passed in, JPEG YCbCr is returned as RGB, edge tiles are cut at the
  image border, and any other compression tifffile knows (deflate, LZW, JPEG 2000, none, ...) works the same
  way. A tile the file leaves out (offset or byte count 0) reads as the page's `nodata` value (0), as in
  `tifffile.asarray`. Integers, slices with steps and negative indices select as numpy would.
- **Axes.** `yxc` for a page with several samples per pixel (RGB; the TIFF `S` axis, in file order), `yx` for a
  single-sample page. dtype is the page's.
- **Access.** `chunk` when the file is read by range. A deflated zip member (or a server without Range) raises
  `NeedsFetch`; the file is then staged whole with `io.fetch` and reported as `file`, and reads still decode
  only the tiles they need.
- **Globs.** A url may be a glob (`<zip>::<glob>` or `<dir>/::<glob>`), but it must match exactly one file.
  Slides differ in shape, so a set of them is not one array: bind one `raw` entry per file. (The harness opens
  one array per binding entry; a snippet cannot return several.)

## Params

- `pixel_size_from_resolution` (optional, default false). With `true`, `voxel_size_nm` is `{y, x}` computed
  from page 0's `YResolution`/`XResolution` and `ResolutionUnit` (inch or centimetre); it fails at open when
  the unit is "none" or a resolution is missing or zero. Without it `voxel_size_nm` is `None`. Slide scanners
  write their pixel size into these tags (the example's slides say 101600 pixels per inch = 250 nm), but in a
  TIFF from other software they are often a print resolution (72 or 96 dpi), which is why the binding has to
  ask and say in `reasons` why the tags can be trusted.

## Known limits

- Page 0 only. The reduced-resolution pages, other series and SubIFDs are ignored; a file whose page 0 is not
  the full-resolution image (flagged reduced) is refused.
- Tiled pages only; a page stored in strips is refused (use `tiff-zip-member-2`). 2D pages with interleaved
  samples only (no `ImageDepth` > 1, no separate sample planes).
- One file per array, so a dataset of many slides needs one binding entry per slide, and each entry costs a
  HEAD and two or three range requests when the binding is opened.
- Network cost is in the harness's 1 MiB blocks: a patch of a few 10-50 kB JPEG tiles fetches the one or two
  blocks that hold them.
- A read assembles the whole bounding box of the selection in memory before applying steps; a full-plane read
  of a slide (`y: slice(None), x: slice(None)`) decodes every tile and needs the decoded size (tens of GB).
  The harness reads `chunk` arrays patch by patch and does not do that for a raw.
- No pixel size from vendor metadata (Aperio ImageDescription `MPP`, Philips XML); only the resolution tags.
- JPEG is lossy and decoded by libjpeg-turbo through `imagecodecs`; values equal `tifffile`'s decode of the
  same page, not necessarily another decoder's to the last bit.

## Example

`examples/s-biad3206-assist-spindle-cell-wsi.yaml`: H&E whole-slide images of cutaneous spindle cell neoplasms
(BioStudies S-BIAD3206), classic TIFF pyramids with 512x512 JPEG/YCbCr tiles, 7 to 10 pages each. Seven of the
410 slides are bound, one raw each: the first slide of each of the four class folders, and from `_1_ood` the
first slide of each centre and the smallest slide. Shapes run from 26576x21392x3 to 75776x179712x3, all uint8
`yxc` at 250 nm per pixel. No labels: the annotations are slide-level diagnoses in a spreadsheet. On
`ASSIST_HCUV_235_A.tif` (30.6 MB, the only slide small enough to decode whole) random 700x700 regions, a
corner, a stepped row and a single channel were compared with `tifffile`'s decode of the whole page and are
identical. The binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
