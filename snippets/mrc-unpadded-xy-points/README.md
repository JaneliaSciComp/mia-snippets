# mrc-unpadded-xy-points

Reads a one-section MRC image without the replicated-edge padding around it, and a headerless text table of
X Y pixel coordinates (picked particle centres in a `.coord` file, for instance) as a `zyx` uint32 label on
that same image. It is one snippet for both because the two must agree on where the image is inside the file:
the raw is the file's pixels from `rows` and `columns`, and the label is the table's points moved by the same
offset.

Why it exists: some micrographs are stored larger than the image they hold, the rest filled by repeating the
image's outermost row and column to the file's edge. A reader that returns the file as it is (`mrc-http-3`)
states that larger extent, and a crop that reaches the fill shows one row or column smeared into a band of
stripes. Those are not detector pixels. This snippet's array is the image only.

- **Urls.** One url, the MRC: the raw image. Two urls, in this order, the points table and then the MRC it
  belongs to: the label. For the label only the first 1024 bytes of the MRC are read (its grid), never its
  pixels. It is the binding's job to give each table the MRC of the same stem and to pair it with that raw.
- **`rows`, `columns`.** `[first, last]`, 0-based and inclusive, in file pixels: where the image is. The array
  is `(1, last_row - first_row + 1, last_column - first_column + 1)` and its pixel `[0, 0, 0]` is the file's
  `[first_row, first_column]`. Without them the array is the whole file. A raw and its label take the same
  values, and smoke's label-vs-raw shape check then holds.
- **The params cannot crop.** A snippet never crops (SPEC §2), so what `rows` and `columns` leave out has to
  be padding, and the reader checks it on the bytes it fetches anyway: in every file row a read covers, the
  columns left out must equal the image's first or last column bit for bit; and a read that touches the
  image's first or last row also fetches the one file row beyond it (16 KB on a 4096-wide float32 file),
  which must equal that row. Otherwise the read raises `ValueError` naming the rows and columns. The check
  sees the rows a read spans plus that one neighbour, not the whole padding; the binding's reasons carry the
  full measurement.
- **Raw.** MRC modes 1 (int16), 2 (float32), 6 (uint16) and 12 (float16), either byte order, axis order
  1 2 3, one section; values as stored. `access` is `chunk`: a read is one range request for the full-width
  file rows it spans. `voxel_size_nm` is from the header's cell, or `None` when the cell is 0.
- **Table.** ASCII text; every non-blank line is two non-negative integers separated by spaces or tabs, X then
  Y. Anything else (a header line, a third column, a decimal point, a minus sign) is refused with the line
  number, so EMAN `.box` files (four columns, box corners) and STAR files are not read by accident.
- **Coordinates.** X is the column and Y the row of the file, 0-based, Y counted from the first row stored
  (no flip): a point marks the file pixel `[Y, X]`, which is `label[0, Y - first_row, X - first_column]`.
  Checked on EMPIAR-10017 (see the example binding's reasons): with this reading the picks sit on the
  particles, and with Y or X flipped or swapped they do not. A point outside the file's grid is refused. A
  point inside the file but in the padding is kept: with `radius` 0 it marks nothing, and with a radius the
  part of its disc that reaches the image is marked. `n_in_padding` counts them after the first read.
- **Values.** A marked pixel holds the 1-based number of its point among the table's points (line number,
  when there are no blank lines); 0 is everywhere else. Ids are per table. Bind it as `segmentation_type:
  instance` to keep the points apart, or `binary` for a {0,1} marker mask.
- **`radius`** (label only, default 0). With 0 each point marks exactly one pixel. With `radius: r` every
  pixel within `r` pixels of a point is marked, a disc `2r + 1` pixels across; where discs of two points
  overlap a pixel takes the nearer point, and the earlier line on a tie. A point listed twice shows only its
  first id.
- **Label access.** `file`: opening reads the MRC header and the table's size; the first `read` reads the
  whole table (kilobytes) and keeps the points in memory. Later reads cost no bytes. `voxel_size_nm` is
  `None`: a points table states none.

## Params

- `rows`, `columns` (lists of two ints): the image inside a padded file, above. Measure them, do not guess:
  the first and last file row that differs from its inner neighbour, and the same for columns, on whole
  files. The reason in the binding says how they were measured and on how many files.
- `radius` (int, pixels, default 0): the marker size, above. The table gives positions only, so any radius is
  the binding's choice and needs a reason; it is not the object's size. Single pixels are the faithful
  reading, but they are dropped by nearest-neighbour downsampling and are hard to see in a preview; a small
  disc survives both.

## Known limits

- Replicated-edge padding only. Padding with a constant (zeros, the mean) fails the check unless the image's
  edge happens to hold that value; that needs another snippet.
- One section, axis order 1 2 3, no mode 0 (its signedness needs `mrc-http-3`'s IMOD handling). A table picked
  on a stack, or on a TIFF, needs another snippet.
- Array coordinates are image coordinates: a crop's `y`, `x` are `first_row`, `first_column` less than the
  file's row and column, and the table's X, Y likewise.
- 0- against 1-based coordinates cannot be told from a table; the snippet takes them as 0-based. On EMPIAR-10017
  every coordinate is a multiple of 3, so one pixel is below the precision of the picks.
- Integer coordinates only; sub-pixel positions (decimal points) are refused, not rounded.
- A table over 16 MiB is refused as "not a points table" (it catches swapped urls before anything is read).
- Points, not outlines: with any `radius` the label marks centres and says nothing about the object's extent.
  Unmarked pixels are "no point here", which is not the same as "no object here" unless the picking was
  exhaustive.

## Example

`examples/empiar-10017-beta-galactosidase-particle-coordinates.yaml`: 84 Falcon-II micrographs and their 84
`.coord` tables, 40863 manually picked beta-galactosidase particles, one instance label per micrograph with
`radius: 12`.

Every one of the 84 files is 4096×4096 float32, but the micrograph in it is 3914×3921: file rows 96..4009
and columns 68..3988. The other 8.5% of each file, 96 rows above, 86 below, 68 columns left and 107 right,
repeats the micrograph's edge pixels, identically placed in all 84. An earlier head of this pull request
read the raw with `mrc-http-3`, whose array is the whole file, and review saw the result twice: crops with a
band of one repeated row or column. Both guesses in that review were reasonable and neither was the cause
(the reader returned every pixel of its stated extent and no crop ran past it); the stated extent itself was
larger than the image. The binding now reads raw and labels with `rows: [96, 4009]`, `columns: [68, 3988]`,
so both are `(1, 3914, 3921)` and no crop can reach the padding.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
