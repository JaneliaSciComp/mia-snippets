# xy-points-mrc-grid

Reads a headerless text table of X Y pixel coordinates, one point per line (picked particle centres in a
`.coord` file, for instance), and returns the points as a `zyx` uint32 label on the grid of the MRC image they
were picked on. A table holds positions, not pixels, so like `imod-model-mask` this snippet computes its planes
in `read`; it does so the same way for every read, from the table alone.

- **Urls.** Exactly two, in this order: the points table, then the MRC image it belongs to. The table does not
  say how large its image is, so the grid comes from the MRC header: `shape` is `(1, ny, nx)`, as `mrc-http-3`
  presents a one-section file. Only the first 1024 bytes of the MRC are read, never its pixels. Smoke's
  label-vs-raw shape check then holds by construction, so it is the binding's job to give each table the MRC
  with the same stem and to pair it with that raw.
- **Table.** ASCII text; every non-blank line is two non-negative integers separated by spaces or tabs, X then
  Y. Anything else (a header line, a third column, a decimal point, a minus sign) is refused with the line
  number, so EMAN `.box` files (four columns, box corners) and STAR files are not read by accident.
- **Coordinates.** X is the column and Y the row of the array `mrc-http-3` returns for the MRC: Y counts from
  the first row stored in the file, with no flip, and both are 0-based, so a point marks `label[0, Y, X]`.
  Checked on EMPIAR-10017 (see the example binding's reasons): with this reading the picks sit on the
  particles, and with Y or X flipped or swapped they do not. A point outside the grid is refused; it means the
  table belongs to another image or uses another convention.
- **Values.** A marked pixel holds the 1-based number of its point among the table's points (line number,
  when there are no blank lines); 0 is everywhere else. Ids are per table. Bind it as `segmentation_type:
  instance` to keep the points apart, or `binary` for a {0,1} marker mask.
- **`radius`** (default 0). With 0 each point marks exactly one pixel. With `radius: r` every pixel within `r`
  pixels of a point is marked, a disc `2r + 1` pixels across; where discs of two points overlap a pixel takes
  the nearer point, and the earlier line on a tie. A point listed twice shows only its first id.
- **Access.** `file`: opening reads the MRC header and the table's size; the first `read` reads the whole
  table (kilobytes) and keeps the points in memory. Later reads cost no bytes. `n_points` is set after the
  first read.
- **Voxel size.** Always `None`: a points table states none.

## Params

- `radius` (int, pixels, default 0): the marker size, above. The table gives positions only, so any radius is
  the binding's choice and needs a reason; it is not the object's size. Single pixels are the faithful
  reading, but they are dropped by nearest-neighbour downsampling and are hard to see in a preview; a small
  disc survives both.

## Known limits

- The image must be an MRC with one section and axis order 1 2 3. A table picked on a stack, or on a TIFF,
  needs another snippet.
- 0- against 1-based coordinates cannot be told from a table; the snippet takes them as 0-based. On EMPIAR-10017
  every coordinate is a multiple of 3, so one pixel is below the precision of the picks.
- Integer coordinates only; sub-pixel positions (decimal points) are refused, not rounded.
- A table over 16 MiB is refused as "not a points table" (it catches swapped urls before anything is read).
- Points, not outlines: with any `radius` the label marks centres and says nothing about the object's extent.
  Unmarked pixels are "no point here", which is not the same as "no object here" unless the picking was
  exhaustive.

## Example

`examples/empiar-10017-beta-galactosidase-particle-coordinates.yaml`: 84 Falcon-II micrographs
(4096×4096 float32 MRC, read by `mrc-http-3`) and their 84 `.coord` tables, 40863 manually picked
beta-galactosidase particles, one instance label per micrograph with `radius: 12`. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
