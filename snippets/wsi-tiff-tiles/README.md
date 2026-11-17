# wsi-tiff-tiles

Reads the full-resolution level of one whole-slide image that is a **tiled TIFF** at a plain HTTP(S) URL:
Aperio SVS (classic or BigTIFF, as Grundium and Leica scanners write it) and Hamamatsu NDPI. A colour slide is
`cyx` with the colour samples as `c` (R, G, B for an H&E scan); a grey one is `yx`.

A slide is gigabytes decoded (2.4 GB to 73 GB for the slides this was tested on), and `tiff-zip-member-2`
decodes a page whole for any region. This snippet never does: `read` fetches by range and decodes **only the
tiles the region touches**, so a 256 px patch costs the 1 to 4 tiles under it, and `access` is `chunk`.

- **Level 0 only.** The array is the file's first IFD, which is the full-resolution page of an SVS and of an
  NDPI. The pyramid's other pages (reduced levels, thumbnail, label, macro) are never visited, so `open_array`
  costs one HEAD and the blocks holding the first IFD and its tile table (2 to 5 MiB per slide here).
- **Tiles.** tifffile supplies the page's tile offsets and decodes each tile (JPEG with the page's JPEGTables,
  YCbCr converted to RGB; any other compression tifffile decodes works the same way, only JPEG was tried on
  real slides). Tiles on the right and bottom edges are cut to the image. A tile stored with no bytes reads as
  0, as tifffile fills it.
- **NDPI.** Level 0 of an NDPI is one JPEG stream with restart markers, not TIFF tiles. tifffile presents its
  restart intervals (the `McuStarts` tag) as tiles, typically 8 px high and a few thousand wide, and this
  snippet reads those. An NDPI page without that tag (a small slide stored as a plain JPEG) is refused as
  "not tiled".
- **One slide, one array.** Slides differ in shape, so a folder of them is not a stack: bind **one raw per
  slide**. The URL is the slide's file. It may also be a glob over an HTTP directory index,
  `<dir url>/::<glob>` (`fnmatch` syntax, resolved by `io.expand`), with **`params.index`** naming which match
  (0-based, in name order) this array is; a glob that matches several files without `index` is refused. Prefer
  plain file URLs when the names are known: a crop's provenance then names the slide itself, and a listing
  that changes cannot move an index onto another slide.
- **Reads.** The tiles of a region that lie within 1 MiB of each other in the file come from one read (up to
  32 MiB at a time), so `io` asks for adjacent blocks in one request. `io` fetches in 1 MiB blocks, which is
  the floor of what a patch costs: a few MiB, not the tiles' own tens of kilobytes.
- **dtype** is the page's (uint8 for these scans). **Voxel size** is what the scanner wrote: the `MPP` field
  of an Aperio ImageDescription (microns per pixel, the same for y and x), or an NDPI's X/YResolution tags
  when their unit is centimetres. Any other TIFF gives `None`; plain resolution tags describe print size.
- A server without Range support raises `NeedsFetch` and the whole file is staged with `io.fetch`
  (`access: file`), which the job's byte cap will refuse for a large slide.

## Known limits

- Level 0 only: no reduced levels, no label or macro image, no choice of series.
- 2D pages with contiguous samples (`YXS` or `YX`); a stripped page, separate planes or a z-stack is refused
  at open. Stripped TIFFs belong to `tiff-zip-member-2`.
- The harness crops one channel at a time, so a crop of a colour slide is one of R, G or B as grey.
- Most of a slide is empty glass. The snippet reads what is asked; it does not look for tissue. A crop job
  finds tissue only through the harness's content filter, each rejected candidate costing its tiles.
- A plain URL only (HTTP or S3 through `io`); zip members were not tried.
- Slide-level annotations (a grade per slide in a table) are not label arrays and are not read.

## Example

`examples/s-biad2323-par-prostate-biopsy-wsi.yaml`: the PAR dataset (BioImage Archive S-BIAD2323), 339 H&E
prostate needle biopsies each scanned on a Grundium (SVS BigTIFF), a Hamamatsu (NDPI) and a Leica (SVS)
scanner, JPEG/YCbCr, 512 px tiles in the SVS files. Bound are four slides, one raw each: biopsy C021B from all
three scanners (`c021b_grundium.svs` 3×28672×28672, `c021b_leica.svs` 3×30002×34281, `c021b_hamamatsu.ndpi`
3×110336×222208) and `c001a_grundium.svs` (3×81920×57344, 14 GB decoded). `harness smoke` on the four fetched
11.5 MB in all. No labels: the dataset's only annotation is `slide_labels.tsv`, Gleason and ISUP grades per
slide.

What was checked on these files:

- Pixel size as each file states it: 250.552 nm (Grundium `MPP`), 263.516 nm (Leica `MPP`) and 226.588 nm
  (NDPI resolution tags), against 0.25 / 0.26 / 0.22 um on the BioStudies record.
- Geometry and colour, by reading a level-0 region holding tissue through this snippet, averaging it down and
  comparing it with tifffile's own whole-page decode of a reduced page of the same file: correlation per
  channel 0.9998 or more for the NDPI (2048 px region against the 1/32 level), 0.997 or more for the Leica SVS
  (against the 1/4 level), and 0.93 to 0.95 for the Grundium SVS against its 896 px thumbnail, the only
  reduced page compared for that file (a thumbnail is not an exact level).
- `harness crop --n 8` over the four slides made 8 crops with 7 content rejections for 52 MB. Seven were on
  the NDPI, by far the largest array, and all eight were glass or slide border, not tissue (see Known limits).

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
