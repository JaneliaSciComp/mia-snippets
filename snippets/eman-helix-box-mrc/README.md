# eman-helix-box-mrc

Reads one EMAN helix `.box` text file as a label on the grid of the MRC stack its helices were boxed on: a `zyx`
uint16 array of instance ids, 0 for background and `k` for the `k`-th helix of the file (1-based, file order).
A `.box` file holds coordinates, not pixels, so `read` computes its planes, the same way for every read.

- **Urls.** Exactly two, in any order: the `.box` file (told by its extension) and the MRC it pairs with. The
  `.box` text states no image size, so the grid is the MRC's header dims. Only the first 1024 bytes of the MRC
  are read (through `io`, so that is one 1 MiB block); the `.box` file is read whole at open.
- **The text.** Rows of five whitespace-separated integers `x y w h flag`, two rows per helix: flag `-1` (the
  box at the helix's start point) then flag `-2` (the box at its end point). `x, y` is the corner of a `w` x `h`
  box. This is what EMAN1 `boxer` and EMAN2 `e2helixboxer` write for helices: each end point `(px, py)` as the
  square box `px - w/2, py - w/2, w, w`. Blank lines are skipped. Anything else is refused with the line number:
  another column count, non-integers, flags that are not `-1` then `-2` (a file of particle boxes), an odd row
  count, boxes that are not equal squares, a helix whose two ends are the same point.
- **A helix.** The rectangle whose axis runs from the centre of the start box `(x + w/2, y + w/2)` to the centre
  of the end box and whose width is the box edge `w`. Pixel (column `i`, row `j`) gets the helix's id when the
  point `(i, j)` lies in that rectangle (edges included). The rectangle ends at the two centres; it is not
  extended by half a box beyond them. Where helices cross, the later one in the file is on top. A helix whose
  end point lies outside the MRC's grid is refused: the file was made on another image.
- **Rows.** `x` is the column and `y` the row in the MRC's stored order (row 0 is the first row in the file),
  the order `mrc-http-3` returns, which is how EMAN indexes an image. No flip. Checked on EMPIAR-10022, see below.
- **Frames.** `shape` is the MRC's `(nz, ny, nx)` and every z plane is the same raster. The MRC here is a movie
  stack of dose-fractionated frames of one micrograph, and the helices were boxed once on that micrograph, so
  the annotation of every frame is the same. That makes the label match a raw that reads the frames as z (smoke
  compares every axis but c). The snippet does not look at what the MRC's z is: bound to a tomogram or a stack
  of different micrographs it would repeat the plane just the same, which would be wrong. Bind it only to a
  stack whose planes are frames of the micrograph the `.box` belongs to.
- **Access.** `file`. After open nothing more is fetched; a read rasterizes only the rows and columns asked for.
- **Voxel size.** Always `None`; a `.box` file states none.

## Params

None.

## Known limits

- The label is the picker's box, not the outline of the filament. On EMPIAR-10022 every box is 260 px wide
  (27.6 nm at the record's 0.1062 nm/px) around a TMV rod of 18 nm, so about a third of each rectangle's width
  is background, and the rod may sit a few pixels off the axis. A consumer must not take the rectangles for
  pixel-accurate masks.
- A helix is one straight segment. A filament boxed as several segments is several ids.
- Beam-induced drift between the frames of a movie (a few pixels) is ignored: one raster for all frames.
- Instance ids only. A binary mask is `!= 0`; the file names no class, so there is no semantic form.
- Ids are the helix's number in its own file; they are not unique across files.
- Only the helix form of `.box`. Particle box files (no `-1`/`-2` flags), EMAN2 `.json` box databases, RELION
  `.star` and other coordinate tables are refused or unread.
- MRC axis order `mapc/mapr/maps` 1 2 3 only, no check of the MRC beyond its header dims.

## Example

`examples/empiar-10022-tmv-falcon2-embl-helix-boxes.yaml`: one 7 x 4096 x 4096 float32 movie stack read by
`mrc-http-3` and its `.box` file (9 helices). What was checked while testing:

- All 109 `.box` files of the dataset open through the snippet against the header of their `<stem>_stack.mrc`
  with no refusal: 928 helices, every box 260 x 260, every stack header 7 x 4096 x 4096. So the format
  rules above hold for the whole dataset.
- Row order, on pixels: on frame 3 of the sample stack, rows 512..2560, the mean intensity inside each helix's
  rod (within 70 px of the axis) minus that of its flanks (110..250 px from the axis) is negative for all 7
  helices that cross those rows (-0.7 to -3.2 counts on a mean of 398: the rod is darker than the ice beside it)
  when `y` is read as the stored row. With `y` flipped, `x` flipped, both, or `x` and `y` swapped, the same
  measure has mixed signs. The server was too slow to read whole stacks, so this is one frame of one micrograph.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
