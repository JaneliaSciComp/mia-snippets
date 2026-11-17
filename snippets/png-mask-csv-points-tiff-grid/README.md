# png-mask-csv-points-tiff-grid

Reads one **label file that is not on its image's pixel grid by itself** and returns it as a `yx` array with
the shape of the single-page TIFF it annotates. Two kinds of label file:

- a **CSV points table** (`x,y`, one object per row): positions, not pixels;
- a **greyscale PNG mask of bit depth 1** (or 8) stored on a `k` times coarser grid than the image.

It does not read the image itself; bind the TIFF as a raw with a TIFF snippet (`tiff-zip-member-2`).

## URLs

Always **two**: the label file and the TIFF whose grid it is placed on, in either order. Each is a plain URL
or a zip member `<zip url>::<member>`. They are told apart by their first bytes (TIFF magic, PNG signature;
anything else must be the CSV), never by name. No globs: a label needs its own TIFF named beside it.

The TIFF must be one series of one page with axes `YX`; only its header is used (`shape`), never its pixels.
A member that cannot be read by range (a deflated one) is staged whole with `io.fetch`, the TIFF included, so
opening a label costs the TIFF's compressed size once; `io` keeps it, and the raw of the same binding reuses it.

## CSV points table (no params)

- **File.** Text, first line the header `x,y` (case and spaces ignored; anything else is refused at open),
  then one `x,y` pair of numbers per line, `\n` or `\r\n`. Blank lines are skipped. A line that is not two
  finite numbers raises, naming the line.
- **Array.** `uint32`, the TIFF's shape. Point number `i` (the 1-based data row, the header not counted) lies
  in the pixel at row `floor(y)`, column `floor(x)`, and that pixel's value is `i`. Every other pixel is 0.
  So `x` runs along the image's width and `y` down its height, the origin is the top-left corner and pixel
  `n` covers `[n, n + 1)`.
- **One pixel per point.** The table states a position and no extent, so the snippet draws none: no disc,
  no radius. Ids are row numbers, so `segmentation_type: instance` keeps the objects apart and `binary` gives
  a 0/1 marker image. Either way it is a marker, not a mask of the object.
- **A point outside the TIFF** (after flooring: `x < 0`, `x >= width`, `y < 0`, `y >= height`) raises at the
  first read with the count and the first such row: the table is then not in this TIFF's pixels.
- **Two points in one pixel:** the later row's number is kept. After the first read the array says how many
  points there are and how many were overwritten this way (`n_points`, `n_overwritten`).
- **Access `file`.** Open reads the header line; the first `read` parses the table and keeps the points. A
  read builds only the region asked for.

## PNG mask (`label_factor: k`, required)

- **File.** PNG colour type 0 (greyscale), bit depth 1 or 8, no alpha. Palette, RGB, 2-, 4- and 16-bit files
  are refused at open.
- **Values.** The stored samples, `uint8`. A **1-bit** PNG reads as **0 and 1**. (The decoder, libpng through
  `imagecodecs`, hands 1-bit samples back either as stored or widened to 0 and 255; the snippet returns
  `decoded > 0`, which is the stored bit in both cases, and raises if it sees any other value.) An 8-bit
  PNG's values are returned unchanged.
- **Placement.** The array has the TIFF's shape and

  ```
  label[y, x] = png[y // k, x // k]
  ```

  i.e. PNG pixel (i, j) covers TIFF pixels (i·k … i·k+k−1, j·k … j·k+k−1). The PNG's shape must equal the
  TIFF's divided by `k`, **rounded up**, on both axes (6669 x 7641 with `k = 5` needs 1334 x 1529); anything
  else raises at open, so a wrong factor or a wrong pairing cannot produce a shifted mask. When the TIFF's
  size is not a multiple of `k`, the last PNG row or column covers fewer than `k` TIFF pixels. `k = 1` is a
  mask already on the TIFF's grid.
- **Access `file`.** Open reads the PNG's 33-byte header; the first `read` decodes it whole and keeps it.

**Why this is in a reader.** A snippet must not resample (SPEC §2), and this one does not: every value is the
file's value at the one PNG pixel that covers the TIFF pixel; nothing is interpolated, averaged or dropped,
and the PNG is recovered exactly by taking every k-th pixel. It states where the file's pixels lie on the raw
grid, as `czi-plane-tiff-grid` does for a coarser TIFF label of a CZI (that snippet rounds the shape down;
this dataset's masks are rounded up) and as the box and contour snippets do for their annotations. If the
harness learns to pair a label by a scale, or to take a points table as a label, bind the files with plain
readers instead and supersede this snippet.

## Voxel size

Always `None`. Neither label file states one, and the TIFF header that gives the grid is not consulted for
it (the tested TIFFs carry the 72 dpi default).

## Known limits

- *Pixel convention of the points is assumed.* `floor` takes integer coordinates as pixel corners. If the
  producer's tool put integer coordinates at pixel centres the true pixel is `round(x)`, and with a 1-based
  origin one less: at most one pixel away, against objects several pixels across. The tested tables do not
  say; checked there only to the eye (rings around the points lie on the stained cells).
- *Origin and edge of the mask are assumed*, as above: PNG pixel (0, 0) covers TIFF pixels (0..k-1, 0..k-1).
  That is what reducing an image by an exact factor with the output size rounded up gives; the tested
  dataset does not document how its masks were made. Another reading (the whole field of view resized)
  differs by under one PNG pixel at the far edge.
- *Mask borders are blocky*: k-pixel staircases on the TIFF's grid.
- Only the header `x,y`. Tables with other column names, more columns, a class or id column, or no header
  are refused, not guessed at; so are quoted fields and separators other than a comma.
- A point label is a single pixel: easy to miss in a preview, and a full plane of it is mostly zeros
  (`uint32`, 4 bytes per pixel when a whole plane is read).
- One label file per array: one slice, one channel. A dataset of many slices needs one binding entry per
  label file, each naming its TIFF.
- Interlaced PNGs go through the same decoder but were not tested. A decode whose shape or dtype is not the
  header's (what a tRNS chunk may cause) raises at read; that path was not exercised either.

## Example

`examples/zenodo-7419283-pnn-pv-mouse-brain-atlas.yaml`: slice `AL1A_002_A1_1` of a 48.7 GB zip (all members
used are deflated): the WFA and parvalbumin TIFFs (6669 x 7641 uint8, read by `tiff-zip-member-2`), the two
point tables (586 perineuronal nets, 568 parvalbumin cells; CRLF lines) and the 1-bit tissue mask
(1334 x 1529, `label_factor: 5`). Checked on these files: every point is in its pixel with its row number,
none collide and all lie inside the tissue mask; the raw is 3 to 5 times brighter at the points than on
average and not when x and y are swapped or an axis is flipped; the mask equals the PNG's stored bits (read
independently with pypng) repeated 5 x 5, and its outline over the reduced raw follows the tissue edge (raw
mean 32.6 inside, 0.35 outside). The example binding's `snippets_commit` is the commit before this snippet's
own (a binding can't name the commit that adds it); a binding written by the worker names a commit that
contains the snippet.
