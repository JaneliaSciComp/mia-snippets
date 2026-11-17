# tiff-csv-contours

Reads one plain **single-page grayscale TIFF** at an HTTP(S) or S3 URL as a `yx` image, and, as its label form,
a **contour CSV** (one row per polygon vertex, in pixels) filled onto the pixel grid of that TIFF as instance ids.

Written for cryo-EM micrographs with per-image vesicle outlines (`<stem>.tiff` and `<stem>-memnet-contours.csv`
on Zenodo). The CSV is parsed with the standard `csv` module and the polygons are filled in `snippet.py` itself
(numpy); the TIFF is read with `tifffile`.

## Raw form: `urls: [<one .tiff>]`, no params

- **What is read.** A TIFF with one series of one page and one sample per pixel: `axes` `yx`, `shape` and `dtype`
  as the page states them. Size-1 axes other than y and x are dropped.
- **Refused at open**, with what the file holds: several pages or series, a z-stack, RGB or any other
  multi-sample image. Those are other snippets' files.
- **Voxel size** is always `None`. A plain TIFF's resolution tags are not a measured pixel size (the tested file
  has `ResolutionUnit` none and no `XResolution`); OME or ImageJ metadata is not looked at.
- **Access `plane`.** At open only the TIFF header and its IFD are read. The first `read` decodes the whole page
  once and keeps it; every region is cut from that. A server without Range support is staged whole (access
  `file`).

## Label form: `urls: [<the .csv>, <its .tiff>]`, `params: {origin: top-left | bottom-left}`

The urls are exactly one contour table, recognised by the `.csv` ending of the url path, and exactly one TIFF as
above, in either order. Only the TIFF's header is read (for the shape of the grid), never its pixels. The CSV is
read whole on the first `read`, so a malformed table fails there, not at open.

- **The table.** The first line must be the three columns `contour_id`, `x [pix]`, `y [pix]` (quoted or not, any
  case); every other non-empty line is one vertex: an integer `contour_id` in 1..65535 and finite x and y. The
  rows of one `contour_id`, in file order, are the vertices of **one closed polygon** (the last vertex joins the
  first). Any other header, a contour with fewer than 3 vertices, or a `contour_id` whose rows are not
  consecutive is an error. A table with a header and no rows is an all-zero label.
- **The array** is `yx` `uint16` with the TIFF's shape. A pixel holds the `contour_id` of the polygon that
  encloses the pixel's centre (even-odd rule), 0 where none does. Ids are the file's own, so they need not be
  consecutive.
- **Coordinates.** x is the column index and y the row index in pixels, with a pixel's centre at its integer
  index. `origin` says where y starts, and has no default because the table cannot say:
  - `top-left`: y is counted down from the top row, vertex row = y.
  - `bottom-left`: y is counted up from the bottom row, vertex row = height − 1 − y.

  **Check it on the image and give the evidence as the binding's reason.** A wrong origin still gives a
  well-formed label of the right shape: it is mirrored top to bottom and lies on the wrong pixels. In the tested
  dataset the record said "y = row" and the data said otherwise (see the example binding).
- **Overlap.** Polygons are painted largest first (by filled pixel count), so where two overlap the smaller one
  is kept, e.g. a vesicle inside another.
- **Off the grid.** Vertices may lie outside the image; the fill is clipped to the grid, and a polygon that
  encloses no pixel centre of the grid adds nothing.
- **Access `plane`.** Every polygon is filled once, inside its own bounding box, and a read pastes the boxes that
  meet the region.

## Known limits

- **Half a pixel.** Whether the table's integers are pixel centres (assumed here) or pixel corners is not
  stated; the two differ by half a pixel in x and y. With `bottom-left` the mirror line has the same doubt
  (row = height − 1 − y here, against height − y − 0.5).
- **An outline is filled up to the line.** For membrane contours the line runs along the membrane, so the region
  holds the vesicle and its membrane up to roughly where the contour was traced.
- **Holes are not expressed.** Each `contour_id` is one simple outline; a self-intersecting one is filled by the
  even-odd rule. Open contours (lines) cannot be told from closed ones and are closed.
- **Overlap and off-grid clipping were tested on synthetic tables only**; the tested file's six polygons do not
  overlap and lie inside the image.
- **One table per label.** One CSV and one TIFF per array; a dataset of many pairs is bound as one raw and one
  label per pair. No globs are taken.
- **Not every object need be outlined.** In the tested micrograph several vesicles have no contour, so 0 means
  "not outlined".

## Tested on

`zenodo-10449491-liposome-cryoem-vesicle-contours-chol`, pair `PA2_00052`: raw `yx` 3838×3710 `float32`; label
6 polygons (ids 1..6, 36 to 55 vertices each, 38,321 to 91,186 pixels) with `origin: bottom-left`. Through this
snippet, the mean of the 7×7-smoothed micrograph on the outer 6 px of each filled region is −26 to −40 (the dark
membrane) and about 0 further inside; with `top-left` it is −5 to +3 on the rim, i.e. background.
