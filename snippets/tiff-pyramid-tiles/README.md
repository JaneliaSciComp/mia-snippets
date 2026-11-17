# tiff-pyramid-tiles

Reads **one page (pyramid level) of a tiled, compressed TIFF** at a plain HTTP(S) URL **tile by tile**: `read`
fetches, by range request, only the encoded tiles that overlap the requested y/x box and decodes those. It
exists for whole-slide images, whose pages cannot be decoded whole: the smallest slide it was written for is
44544×47616 RGB (6.4 GB decoded, 144 MB as JPEG), the large ones about 30 GB. Every other TIFF snippet in the
catalog decodes a page or a file per read.

- **What it returns.** An RGB page (tifffile axes `YXS`) is `cyx`, with c the TIFF's sample axis in file order
  (R, G, B after the decoder's YCbCr to RGB conversion); a grey page (`YX`) is `yx`. dtype is the page's.
  `access` is `chunk`. `read(region)` equals `tifffile`'s `page.asarray()` indexed by the region (checked
  against it on whole reduced pages of the test slide), and nothing is resampled, cropped or normalised.
- **Decoding.** Each tile goes through tifffile's own segment decoder for the page, so JPEG tiles use the
  page's shared `JPEGTables`, `YCbCr` photometric comes back as RGB, and any other compression tifffile
  decodes (LZW, deflate, uncompressed, ...) works the same way. Pages stored in strips are read strip by strip:
  a box then costs the full-width strips it crosses. A tile the file does not store (byte count 0, a sparse
  slide) reads as the page's nodata value, which is 0 unless the file has a `GDAL_NODATA` tag, as in tifffile.
- **`params.level`** (optional, default 0): the 0-based index of the TIFF page to read, in file order. A
  pyramidal TIFF written as one page per level (page 0 full resolution, the next pages its reduced copies with
  `SubFileType` 1) shows up in tifffile as one series per page or a single generic series, so the level is
  chosen by page, not by series. `harness describe <url>` lists the pages. The snippet does not check that a
  page is a level of the same image: a label or macro page at the end of a slide file is a page like any
  other. Levels live in SubIFDs in some files (OME-TIFF pyramids); those are not reachable by `level`.
- **`params.index`** (optional): the urls must stand for one file. A plain url is that file. A glob over an
  HTTP directory index, `<dir url>/::<glob>`, is expanded by `io.expand` and sorted; when it matches more than
  one file, `index` is the 0-based position of the file to read, and without it open fails with the number of
  matches. A listing that gains or loses a file moves every later index, so **one plain url per array is the
  safer way to bind a collection**; `index` is there for a folder that is known to be complete.
- **One file is one array.** The slides of a collection differ in shape, so they cannot be stacked along z.
  Bind each slide as its own raw array.
- **Voxel size.** `voxel_size_nm` is always `None`: plain TIFF resolution tags describe print size, and
  slide files often carry none. It comes from the record, for page 0; a reduced page's is that times its
  downsampling factor (page 0's width divided by the page's).

## Cost

`open_array` reads the 8-byte header and the IFDs up to the chosen page, with that page's tile offset and
byte count tables, and no pixel data. Through `io` that is whole 1 MiB blocks: on the slides it was tested on
(the IFD of each page follows its tiles) 2 MiB per slide at level 0, 3 MiB when the IFD and its tables cross a
block boundary, and at least one more block per level walked.
Binding every slide of a large collection therefore costs 2 to 3 MiB and two requests per slide before the
first crop: 2,502 slides are 5.2 GB or more, over a 5 GB job cap, so bind as many slides as the job's cap
and time allow rather than a whole collection of thousands.

A 256×256 box overlaps 1 to 4 tiles of 512×512; their bytes come in the 1 MiB blocks that hold them. The file
is re-opened for each `read`, which costs nothing new: the header and IFD blocks are in the job's block cache.

## Limits

- Interleaved samples only (`PlanarConfiguration` 1); a page with separate sample planes is refused at open.
- `YX` and `YXS` pages only: no z stacks, no multi-channel pages with more axes.
- The server must answer range requests. A file that would have to be staged whole (`NeedsFetch`) is not
  read: that is what this snippet is there to avoid.
- Open checks the page's layout, not its content; a corrupt tile fails at the `read` that touches it.
- A step that makes requests is tried up to 4 times when the transport fails (timeout, dropped connection,
  5xx or 429), with 1, 2 and 4 s pauses; any other error is raised at once.

## Tested on

`s-biad3510-colorectal-cancer-he-wsi` (BioImage Archive S-BIAD3510): 2,502 H&E whole-slide TIFFs, classic
TIFF, 8 pages each, 512×512 JPEG tiles with shared tables, YCbCr 4:2:0. The example binds four slides of four
tumours at level 0, the record's sample slide first. On the sample slide, pages 5 (1392×1488) and 7 (348×372)
read whole and by a set of sub-regions (stepped, reversed, integer and empty selections) are equal to
`tifffile`'s `page.asarray()`.
