# eman-helix-box-mrc

Rasterises one EMAN helix `.box` file onto the grid of the MRC micrograph or movie stack it was boxed on and
returns it as a `zyx` uint16 label: 0, or the number of the helix that covers the pixel. A `.box` file holds
coordinates, not pixels, so `read` computes its plane (once, from the table alone) instead of fetching it.

## What it reads

- **The table.** Text, one row per line, five whitespace-separated numbers `x y width height flag`. A helix is
  two consecutive rows: flag `-1` for its start and `-2` for its end. This is what EMAN1's `boxer` (helix mode)
  and EMAN2's `e2helixboxer` ("EMAN1 box" coordinates) write: for a helix axis from `(x1, y1)` to `(x2, y2)`
  with box width `w` the rows are `x1 - w/2, y1 - w/2, w, w, -1` and `x2 - w/2, y2 - w/2, w, w, -2`. So `(x, y)`
  is the corner of a `w x w` square centred on one end of the axis, and the end point is `(x + w/2, y + w/2)`.
  A corner can lie off the image (`y = -3`) while its end point is on it.
- **The label.** Each helix is the rectangle EMAN draws for it: the band of width `w` along the axis, from the
  start point to the end point and no further. A pixel belongs to it when its centre is in the band; pixel
  `(row, col)` covers `[col, col + 1) x [row, row + 1)` in box coordinates. Its value is the helix's 1-based
  number in the file (first pair of rows = 1). Parts of a band off the grid are dropped. A helix whose start
  and end are the same point is the `w x w` box itself. A file with no rows is an all-zero label.
- **Coordinates.** `x` is the column and `y` the row of the MRC as stored (row 0 = the first row in the file),
  which is how `mrc-http-3` returns it, so nothing is flipped. EMAN displays that row at the bottom; that is
  display only. Checked on one EMPIAR-10020 stack: the contrast is weak, but in 7 of 7 places measured the
  image is darker on a helix axis than beside it (see the example binding's reasons).
- **The grid.** A `.box` file states no image size, so the binding gives two urls: the `.box` file and the MRC
  it was boxed on (any order; the one ending in `.box` is the table). Only the MRC's 1024-byte header is read:
  `shape` is `(nz, ny, nx)` and `voxel_size_nm` is cell / `m`, exactly what `mrc-http-3` reports for that file
  (same byte-order rule, `mapc/mapr/maps = 1 2 3` only). Bind the label to that same MRC as its raw.
- **z.** The picks are 2D. Every z plane of the label is the same plane, so one set of picks applies to every
  plane of the MRC: every frame of a dose-fractionated movie stack, which `mrc-http-3` opens as `zyx` with the
  frames along z. For a single micrograph `nz` is 1.
- **Access.** `file`. `open_array` reads the MRC header and the whole `.box` file (a few hundred bytes); the
  first `read` rasterises the plane and keeps it, later reads cost no bytes.

## Params

None.

## Binding it

One label per `.box` file, `urls: [<name>.box, <name>_stack.mrc]`, `pairs_with` the raw that reads the same
MRC, `segmentation_type: instance`. The label key's class is what the helices are (the file does not say).

## Known limits

- **Boxes, not outlines.** The band is as wide as the boxer's box, which is chosen larger than the filament
  (EMPIAR-10020: 260 px = 29.3 nm boxes around an 18 nm virus), and it ends where the user clicked. It is a
  filled, rotated bounding box of a helix segment, not a pixel-accurate mask.
- **Overlaps.** Where two bands overlap, the later helix in the file is on top; the earlier one keeps its
  other pixels. No helix is hidden completely in the 14 EMPIAR-10020 files.
- **Not every filament is boxed.** 0 means "not boxed", not "background": the user boxed the straight,
  isolated stretches they wanted to process.
- **One plane for all frames.** Drift between the frames of a movie is not modelled: the picks were made on
  one image of the micrograph (the frame sum, presumably) and are repeated unchanged on every frame.
  Beam-induced motion is a few pixels at most, far below the box width.
- **Helix files only.** Any flag other than `-1` / `-2` (particle boxes), a start without an end, a box that is
  not square, or a start and end of different widths is refused with the line number. EMAN2's own helix
  formats (`x1 y1 x2 y2 width` rows, the `e2helixboxer` database) and SPIDER/RELION `.star` coordinates are
  not read.
- **The MRC must be the one that was boxed.** A `.box` file from a binned or cropped copy has other
  coordinates and would be placed wrongly without an error (the file has nothing to check against).
- At most 65535 helices per file (uint16 ids).

## Example

`examples/empiar-10020-tmv-k2-lmb-helix-boxes.yaml`: 14 K2 movie stacks of tobacco mosaic virus
(22 x 3750 x 3645, float32, read by `mrc-http-3`) and their 14 helix box files, one label per stack.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
