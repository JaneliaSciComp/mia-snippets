# tiff-tiles-csv-points

Reads one multi-page **TIFF whose first series is a stack of single-sample pages** (written for the
PerkinElmer/Akoya **QPTIFF** of a PhenoCycler/CODEX scan: one page per channel, then a pyramid) from a zip member
(`<zip url>::<member>`) or a plain URL; and, as its label form, a **CSV table with one row per object** whose
centroid columns are in micrometres, marked on that image's pixel grid. A points table is not a mask: the label
says where each object's centroid is and what class it has, not which pixels the object covers.

## Raw form: `urls: [<one TIFF>]`, no params

- **What is read.** tifffile's `series[0]` at full resolution. In a QPTIFF that is the `Baseline` series, `cyx`
  with one page per channel in file order; the thumbnail and the reduced-resolution, macro and label pages are
  not part of the array. Axes map `C` → `c`, `Z`/`Q`/`I` → `z`; size-1 page axes are dropped. A series with
  colour (multi-sample) pages or any other axis is refused at open.
- **Reads are by tile.** A read seeks to and decodes only the tiles its region touches (512x512 LZW tiles in the
  example: a 256x256 crop decodes 1 to 4 tiles instead of a 337-megapixel page). A tile the file does not store
  reads 0. An untiled page is decoded whole and the last one is kept.
- **Access.** A stored zip member or a plain file on a server that honours Range is opened with `io.open`
  (`access: chunk` when tiled, `plane` otherwise); only the IFDs and the tiles asked for are fetched. A deflated
  member raises `NeedsFetch` and is staged whole with `io.fetch` (`access: file`), once per dataset cache.
- **Voxel size.** Only for a QPTIFF, which states its pixel size in `XResolution`/`YResolution` with
  `ResolutionUnit` (the example: 13566429/512 px per cm = 377.40 nm). In other TIFFs those tags are a print
  size, so `None`.

Why a deflated member is not inflated by range reads: a deflate stream can only be read from its start, and a
QPTIFF's IFDs are spread through the file (each after its page's tiles), so even listing the 45 channel pages
means inflating past all of them, three quarters of the member or more. Fetching the member whole costs the same
bytes, is CRC-checked, and leaves it in the dataset cache for the next job, where a range reader would download
it again. With the harness's handling of Zenodo's rate limit (429s are waited for) the whole fetch succeeds.

## Label form: `urls: [<one .csv>, <the TIFF>]`

The `.csv` url is recognised by its ending; the other url is the image the table belongs to, read as above but
only for its y and x size and pixel size (no pixel is read). The image must state its pixel size.

- **The array** is `yx` with the image's shape and voxel size, `access: plane`. The table is fetched whole and
  parsed on the first `read`, not at open.
- **Placement.** A point with centroid `(x, y)` µm marks pixel `(floor(y / pixel_y), floor(x / pixel_x))`: the
  table's frame is taken as micrometres from the image's top-left corner, x to the right and y down (QuPath's
  convention). A point outside the grid is an error, since it means the table is in another frame.
- **Values.** With `class_column` and `classes`, the pixel holds the 1-based position of the row's class name in
  `classes` (`uint8`, or `uint16` from 256 classes; bind as `semantic`). A name that `classes` does not list is
  an error. Without them it holds the 1-based number of the data row (`uint32`; bind as `instance`). 0 is
  "no centroid here", not "background". Two points in one pixel: the later row is kept.
- **`radius`** (default 0) also marks the disk of that many pixels around each centroid, for a consumer that
  needs more than one pixel per object. Where disks overlap, a pixel takes one of the overlapping values;
  centroid pixels always keep their own.

## Params (label form only)

`x_column`, `y_column` (defaults `x`, `y`), `class_column` with `classes`, `radius`: see `snippet.yaml`. The raw
form takes none.

## Known limits

- Centroids must be in micrometres; a table in pixels, or an image without a stated pixel size, is refused.
- The table is parsed in memory (the example's 674 MB, 549 columns and 245,719 rows take about 5 s) and the label
  plane is kept in memory (337 MB for the example).
- A quoted field that contains a line break is reported as a row with the wrong number of fields.
- A deflated image member is fetched whole even for the label form, which needs only its header; in a binding
  that also binds the image as raw this is a cache hit.
- Only `series[0]`, full resolution. Channel names (`<Name>` in each page's description) are not part of the
  `Array` interface; the example's are listed below.
- The stored-member and plain-URL paths (`chunk`/`plane` by range), untiled pages, row-number ids and `radius`
  were not run on real data.

## Tested on

`zenodo-23672666-quantcell-bone-marrow-codex` (examples/): `QuantCell_bone_marrow_PhenoCycler.qptiff`, a deflated
member of `raw_image.zip` (1.84 GB, fetched whole in one smoke run of 2,021,122,173 bytes with the table, no
429). 227 pages; `Baseline` series `cyx` 45x11147x30264 uint8, LZW, 512x512 tiles, 377.40 nm. Channels in file
order: dapi1, CD90, Ter119, CD45, CD45R_B220, CD34, CD38, IgD, MHC I, Cd11b, Endomucin, Osteocalcin, Fcgr, CD31,
CD44, CD19, MHCII, CD5, CD21_35, CD4, CD8a, Ki67, CD48, CD105, Flk2, LePr, CD41, CD127, NkP46, TCR, CD117, Par1,
CD3, Ly6g, CD71, CD11c, CD49f, Sca1, Foxp3, SLAM, TH, CD115, F4_80, Perlipin, CGRP.

Tile reads were compared with tifffile's decode of the whole page: equal for an interior box, the bottom-right
corner (partial tiles), the origin, a single row with a step of 7 in x (channel 0) and a two-channel box.

Label: `codex_quantcell_QuantCellPaper.csv` with `class_column: cell_type` and the record's 22 classes. All
245,719 rows land inside the grid (rows 101..11130, columns 1588..30251), each in a pixel of its own, and every
class occurs (from 218 KLS to 82,477 Erythroblasts). Frame check against the DAPI channel: mean intensity is
112.9 at the centroid pixels, 65.7 to 66.9 with every point moved 15 px (5.7 µm) up, down, left or right, 59.4
to 60.3 moved 40 px, 26.6 and 27.3 with y or x mirrored, 8.3 at half the scale, and 14.3 over the whole image;
with x and y exchanged three quarters of the points leave the grid. So the centroids sit on nuclei, in
micrometres from the top-left corner at the file's own pixel size, with no flip, transpose or offset.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
