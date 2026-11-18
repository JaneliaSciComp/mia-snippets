# tiff-wsi-level0-tiles

Reads the full-resolution level of one pyramidal whole-slide TIFF, as `cyx`, fetching only the tiles a region
touches. Written for the VENTANA DP 200 slides of S-BIAD1527: a BigTIFF of 11 pages, `Label_Image`,
`Probability_Image`, then the pyramid `level=0 mag=40 quality=95` to `level=8 ...`, each level RGB `uint8` in
1024 px JPEG tiles. `tiff-zip-member-2` opens such a file too, but decodes the whole level-0 page (3 to 5 GB of
tiles) for any region.

- **URLs.** Exactly one plain URL per array: one slide. No zip member, no glob. Slides differ in shape, so they
  cannot share one array; a binding lists one `raw` entry per slide and the harness draws crops across them.
- **Which page.** The first page whose ImageDescription starts with `level=0`. The label and probability pages
  and the lower levels are not presented. A file without such a page raises.
- **Axes.** `cyx`: `c` is the TIFF sample axis in file order (R, G, B after the JPEG's YCbCr is decoded), `y`
  and `x` the level-0 page's rows and columns.
- **Tile placement.** Tiles go where the TIFF tile grid puts them (tile `i` at row `i // across`, column
  `i % across`, no overlap). That is what these slides' metadata says: the `iScan` XMP on page 0 has
  `Registration Method="None"`, one AOI whose `RectHigh` is the whole level-0 page, and the level-0 page has no
  XMP and so no `TileJointInfo`. tifffile calls the file `bif` and warns that BIF tiles are not stitched; that
  warning is about older BIF files, whose level-0 tiles overlap and are stored in scan order with the overlaps
  in `TileJointInfo`. This snippet refuses a level-0 page that carries `TileJointInfo` rather than misplace its
  tiles. Checked on AFB_NEG_S1_039.tif: a 1024x1024 level-0 window centred on the corner of four tiles,
  averaged 32x32, correlates 0.91 with the same window of the file's own `level=5` page (0.90 to 0.93 in each
  quadrant; below 0.25 with any tile flipped, -0.04 with the tile rows swapped), and the pixel step across the
  seams is that of neighbouring rows and columns.
- **Access.** `chunk`. `open_array` reads the header, the page list up to level 0 and that page's tags and tile
  table, not the 1.8 MB colour profile and no pixels (3 MiB in the harness's 1 MiB blocks for the tested
  slides). `read` fetches each touched tile by its byte range (about 230 kB a tile at JPEG quality 95) and keeps
  the last 32 decoded tiles, so the three `c` planes of a patch decode each tile once. It needs a server that
  honours Range; `NeedsFetch` is not caught, because staging a multi-GB slide whole is not a way to read it.
- **Values** are tifffile's decode of each tile (`imagecodecs` JPEG, YCbCr to RGB), unchanged. The embedded
  ICC profile is not applied. A tile with byte count 0 reads as zeros, as tifffile fills it.
- **dtype.** The page's (`uint8`).
- **Voxel size.** From the level-0 page's `XResolution`/`YResolution` and `ResolutionUnit` (inch or
  centimetre): 40000 px/cm gives `{y: 250.0, x: 250.0}` nm, which agrees with `ScanRes="0.25"` in the XMP.
  `None` when the unit is missing or the value is not positive.
- **Params.** None.

## Known limits

- One slide per array, so a dataset of N slides is N `raw` entries and N opens per job (3 MiB each).
- Only level 0. The stored lower levels are not offered as arrays.
- Slide-level labels (the Positive/Negative folder of S-BIAD1527) are not arrays and are not read.
- Older BIF slides (iScan Coreo/HT: overlapping tiles in serpentine order, `TileJointInfo` in the level-0 XMP)
  are refused, not stitched.
- Tile seams are the scanner's: the slide was assembled without image registration, so a small residual
  misalignment at a seam is in the file, not removed here.
- A full-slide read allocates the whole level (4.7 GB for 43008x36864x3); the tile cache holds 96 MiB.

## Example

`examples/s-biad1527-afb-dx-tuberculosis-wsi.yaml`: four Ziehl-Neelsen slides of S-BIAD1527, one from each of
the four folders, each its own `raw` entry. The other 417 slides bind the same way. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
