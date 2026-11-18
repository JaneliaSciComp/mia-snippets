# tiff-rows-csv-contours

Reads one plain 2D **TIFF** at an HTTP(S) URL (S3 included) as `yx`, and, as its label form, a **CSV of contour
vertices** drawn on that image, each contour filled onto the image's pixel grid as an instance id.

## Raw form: `urls: [<one .tiff>]`, no params

- **What is read.** A TIFF that is one plane of one sample per pixel: `axes` `yx`, `shape` and `dtype` as the file
  states. Several series, pages, channels or RGB samples are refused at open; other TIFF snippets read those.
- **Access `chunk`** when the pixel data lies in the file uncompressed and row after row (strips with nothing
  between them, no predictor): a read fetches only the rows it asks for, by byte range, in the file's byte order.
  Any other TIFF (compressed, tiled) is decoded whole by tifffile on each read (access `plane`). A server without
  Range support is staged whole (access `file`).
- **Voxel size.** OME `PhysicalSizeX/Y` or ImageJ `unit` with `X/YResolution`; `None` when the file has neither.

## Label form: `urls: [<the .csv>, <its .tiff>]`, `params: {y_up: true}` when y runs upward

The urls are exactly one CSV (recognised by the `.csv` ending of the url path) and the one TIFF the contours were
drawn on, in any order. The CSV states no image size, so the TIFF's header gives the grid (shape and voxel size);
its pixels are never read for the label.

- **The CSV** has a header row and one row per vertex, three columns in any order: `x` and `y` (the header cell
  is `x` / `y`, optionally with a unit in brackets, which must be `pix`, `px`, `pixel` or `pixels`: `x [pix]`) and
  one other column, the contour id, an integer in 1..65535. The rows of a contour are together and in order along
  it; a contour is closed from its last vertex back to its first. Anything else is an error naming the row.
- **The array** is `yx` `uint16`. A pixel holds the id of the contour that encloses it, 0 where none does, by the
  even-odd rule. A vertex (x, y) is at column x, row y, in array index coordinates: the pixel `[r, c]` is the
  point (x = c, y = r), so a contour through integer coordinates passes through pixel centres. (A corner-based
  convention would differ by half a pixel, which outlines sampled every 18 px cannot show.)
- **`y_up`.** Absent or false: y is the row, counted down from the top row. `true`: y is counted upward from the
  bottom row, row = height - 1 - y. The CSV does not say which, and a wrong choice gives a plausible label of the
  right shape, upside down. Decide it on evidence and give the evidence in `reasons`: the image under the
  contours differs from the image elsewhere only one way up (see "Tested on").
- **Overlaps.** Where contours overlap, the one enclosing fewer pixels is kept (ties: the lower id), so a contour
  inside another stays visible and the outer one keeps the rest.
- **Clipping.** A contour partly outside the image is clipped to it; one wholly outside is an error (wrong image
  or wrong frame). A contour with fewer than 3 vertices is an error. A CSV with a header and no rows is an empty
  label. A contour too small to enclose a pixel contributes nothing.
- **Access `plane`.** The CSV is read whole on the first `read` and kept as one mask per contour.

## A host that is slow or failing

Every step that makes requests (the TIFF's header at open, the rows of a read, the CSV on a label's first read) is
tried again after 10, 30 and 90 s when the harness's client gives up on it in transit: a timeout, a dropped or
cut connection, a 5xx or 429 answer. The client itself asks four times over about 20 s, with 60 s for each
answer; a host can be out for longer, and a crop job opens every array of a binding before it makes its first
crop, so a single such request ends the job with no crops (that happened on Zenodo: see "Tested on"). Blocks
fetched before the failure stay in the job's block cache and are not asked for again, and a failed request is
not charged to the byte cap. Any other error (a 404, a host that is not allowed, the byte cap, a file that is
not a TIFF) is raised at once, and a host that stays down fails with the client's own error after the last wait.

A binding of n pairs costs 3 requests per TIFF at open (HEAD, the block with the header, the block with the IFD
when that is at the file's end), 2 per CSV on its first read, and one per read of up to 8 MiB of rows.

## Known limits

- Tested on real data: little-endian uncompressed float32 strips, and `y_up: true` contours that neither touch
  the image border nor nest deeply. Big-endian, multi-strip, compressed and tiled TIFFs, the staged (`file`) path,
  ImageJ voxel sizes, `y_up: false`, clipping, nesting, every refusal and the waits (below) were run on synthetic
  input only.
- One CSV per label. Holes are not modelled: a contour inside another is a second instance, not a hole.
- The fill is of pixel positions strictly by geometry; a program that rasterizes outlines differently (e.g. by
  drawing the outline, then filling) differs along the outline's pixels.
- Open contours (lines) cannot be told from closed ones in such a CSV and are closed like the rest.

## Tested on

`zenodo-10449449-liposome-cryoem-vesicle-contours` (examples/): 12 cryo-EM micrographs of liposomes, each a
single-page uncompressed float32 TIFF of 3838 x 3710, and for each a MEMNET (TARDIS) contour CSV with header
`"contour_id","x [pix]","y [pix]"`: 336 contours in all, 13 to 47 per micrograph, ids 1..n, 29 to 62 vertices
each about 18 px apart, all inside the image. The whole plane of S3_00007 read by row ranges equals
`tifffile.imread` of the same file.

Frame check, on all 12 pairs: the mean micrograph value (5 px box) along the contours is -0.6 to +0.2 with y
taken as the row, and -6.1 to -27.0 in every micrograph with row = 3837 - y (the membrane is dark). On S3_00007
the profile across the mirrored contours shows the two dark leaflets of the bilayer 12 px apart, 4 px inside and
8 px outside the contour (-38.7 and -19.2 against +1.9 at 30 px either side); with x and y exchanged, x mirrored,
or the contours moved 60 px it is -0.3 to +0.5. So the CSVs' y runs upward and the binding sets `y_up: true`.
The check places the contours to about a pixel: row = height - y cannot be told from row = height - 1 - y.

One pair of contours overlaps (S3_00014, ids 6 and 7: 7 lies almost wholly inside 6), which leaves id 6 as a
453-pixel rim; for every other contour the labelled pixels equal the polygon area to within 1 %.

Zenodo, slow and failing: the first crop job on this binding ended with `ReadTimeout` while it opened the fifth
TIFF, after 14 requests, with no crops. When the retries were added (2026-11-18) its answers took 0.4 to 33 s to
start and about one request in six failed (504 after 60 s, 500, 502), HEAD, whole and ranged requests alike.
With the retries, smoke of all 12 pairs passed in 21 min, and a 6-crop job made 6 crops in 16 min, all of which
verify; in that job six requests failed on their first try and the client's own second try got each one, so the
snippet's waits did not come into it. They were run against a local server only: 503s and timeouts that outlast
the client's four tries are waited out and the read then equals the file; a host that stays down fails with the
client's error after the 90 s wait; a 404, a non-TIFF and the byte cap are raised with no wait.
