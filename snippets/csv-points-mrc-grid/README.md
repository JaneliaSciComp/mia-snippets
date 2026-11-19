# csv-points-mrc-grid

Reads a **CSV table of particle centres** as a label on the grid of the **MRC micrograph** the particles were
picked on. A table holds positions, not pixels, so `read` paints its plane: `zyx` `(1, ny, nx)` `uint16`, 0 for
background and, at each particle's centre pixel, the particle's **1-based row number** in the table (ids restart
in every file). The axes are the ones `mrc-http-3` gives a single-section micrograph, so the label pairs with it.

- **The table.** Comma-separated text with a header line that has exactly one `X-Coordinate` and one
  `Y-Coordinate` column, in micrograph pixels; one row per particle. This is the `particle_coordinates/*.csv`
  layout of CryoVirusDB (and of CryoPPP, from the same group): `X-Coordinate, Y-Coordinate, Diameter, Angle-Psi,
  Origin X (Ang), Origin Y (Ang), Defocus U, Defocus V, Defocus Angle, Phase Shift, CTF B Factor`. Only the two
  coordinate columns are read. A BOM, CRLF line ends and blank lines are accepted; a table with only its header
  is an all-background plane.
- **URLs: two, in this order.** The table, then the micrograph (`.mrc`) it belongs to, normally the URL of the
  raw the label pairs with. Only the micrograph's 1024-byte header is read (one 1 MiB range request), for
  `nx`, `ny` and `nz`; it must have one section and axis order 1 2 3. No globs.
- **`y_axis` (required): `up` or `down`.** Columns are `X`. Rows are counted against the MRC as stored, i.e. as
  `mrc-http-3` returns it: `down` is `row = Y`; `up` is `row = ny - 1 - Y`, Y counted from the last stored row
  (the bottom-left origin many cryo-EM programs use). **The table does not say which, and a wrong choice still
  gives a label that passes every shape check**, mirrored top to bottom. So the binding must decide it on the
  data and say how: average the raw around the centres both ways (see the example); the right way shows the
  particle, the other is flat.
- **Coordinates are pixel indices**: column `floor(X)`, and `floor(Y)` before the flip. All coordinates in the
  tested files are whole numbers.
- **`radius` (optional, default 0).** 0: a particle is its centre pixel and nothing else. `N` (1..2048): every
  pixel whose distance to a centre pixel is `<= N` gets that particle's id; a pixel in reach of several
  particles goes to the nearest centre, the lower id on a tie; discs are clipped at the image border. It is the
  size of a mark that a binding chooses so the points can be seen or trained on, **not the particle's extent**.
- **The `Diameter` column is not used.** In CryoVirusDB's EMPIAR-10033 files it is 350 in every row, while the
  particles measure about 220-260 px across in the micrograph (dark disc to r ≈ 110 px, bright fringe at
  r ≈ 130-150 px). It is a picking parameter in an unstated unit, not a per-particle size in pixels.
- **Refused, at open** (the whole table is checked there, naming the file and the row): a centre outside the
  micrograph; two rows on the same pixel (one particle would be lost); a row without numbers or with a
  non-finite coordinate; a header without the two columns; a table that is not text or is larger than 16 MiB;
  a micrograph with several sections, another axis order, or a header that is not MRC; more than 65,535 rows.
- **Access.** `file`: `open_array` reads the micrograph header and the table (kB) and no pixels. The plane is
  painted at the first `read` and kept (32 MB for 4096x4096); later reads cost no bytes.
- **dtype** `uint16`. **Voxel size** `None`: the table has none.

## Binding it

One label per micrograph, `pairs_with` its raw, `segmentation_type: instance` (each particle its own id; the
binding schema has no points type). With `radius` 0 a 256x256 crop holds at most a few marked pixels; the
harness finds them for its annotation-aimed crops, but an overlay shows single pixels.

## Known limits

- Whether `up` means `ny - 1 - Y` or `ny - Y` (and whether X/Y are pixel indices or pixel edges) cannot be told
  from particles hundreds of pixels wide; the mark may be one pixel off.
- Column names are fixed. A table with other names, tabs or semicolons, or a STAR file, needs a new snippet.
- One table and one single-section MRC per array; no stacks, no globs, no micrograph in another format, no
  micrograph inside an archive.
- Only positions: `Angle-Psi`, the origin offsets and the CTF columns are dropped. Only the rows of the table
  bound are in the label; picks kept elsewhere (an "excluded" list, false positives) are background.
- `radius` was tested on synthetic tables and by reading the example's labels with it, not in a published
  binding.

## Example

`examples/zenodo-10397742-cryovirusdb-virus-particles.yaml`: the 22 EMPIAR-10033 micrographs of CryoVirusDB's
`sample_data/` (4096x4096 float32 MRC, `mrc-http-3`) and their 22 coordinate CSVs (16 to 134 particles each,
1721 in all), `y_axis: up`.

How `up` was decided: the raw was binned 8x, z-scored and averaged in a window around every centre, with the
label read both ways through this snippet.

| micrograph | y_axis | particles | r < 80 px | r 120-160 px | r 240-320 px |
|---|---|---|---|---|---|
| `..._224220` | up | 67 | -0.19 | +0.53 | +0.27 |
| `..._224220` | down | 67 | +0.02 | -0.04 | -0.04 |
| `..._234508` | up | 134 | -0.48 | +0.32 | -0.05 |
| `..._234508` | down | 134 | +0.04 | +0.01 | +0.03 |

`up` shows a dark particle inside a bright fringe on both micrographs; `down` is flat, as are x mirrored and
x/y swapped (checked on `..._224220`).

Tested with `harness smoke` (22 raws and 22 labels of 1x4096x4096, no errors; the only warnings are the label
dtype, which the record gives as `unknown`) and `harness crop --n 4` (4 crops made, labels written as 16-bit
PNGs with one particle id in each crop that holds a centre). Behaviour on malformed input was tested on
synthetic tables and MRC headers over a local range server. The example binding's `snippets_commit` is the
commit before this snippet's own (a binding can't name the commit that adds it); a binding written by the
worker names a commit that contains the snippet.
