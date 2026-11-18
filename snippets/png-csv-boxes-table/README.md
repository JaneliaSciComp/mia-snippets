# png-csv-boxes-table

Reads a set of 2D PNG images that a CSV table lists, one table row per z plane in row order (`z` is kept for a
one-row table too). The **one url of every array is the table**; each row names its image file in
`name_column`, and `image_url` says where a file of that name is. An array is either

- **the images** (no `boxes_column`): each row's PNG, decoded with `imagecodecs`. Samples are kept: an RGB file
  is three `c` planes (`zcyx`, c = R, G, B), a grey file none (`zyx`); or
- **the boxes of one column** (`boxes_column`): each row's cell is a Python-literal list of dicts
  `{'top': .., 'left': .., 'height': .., 'width': ..}`, optionally with a `'type'` string, in pixels of that
  row's image. The cell is rasterised (`zyx`, `uint16`) on the grid of the row's PNG, whose header is read for
  it.

It exists because the two box snippets (`tiff-jpeg-voc-zip-glob`, `jpeg-xml-boxes-numbers`) read one XML file
per image, and because a file store without a directory index (Zenodo's files API) cannot be globbed: here the
table itself supplies the file names.

- **`image_url`** is a template holding `{name}` once, e.g.
  `https://zenodo.org/api/records/<id>/files/{name}/content`. The name from the table is put in as it is, and
  must be a bare file name (letters, digits, `_ . + -`): a cell with a path, a query, a glob or a zip separator
  raises, so the table cannot point the reader anywhere but at siblings named by the binding's template. `io`
  still checks every host.
- **`name_column`**, **`delimiter`**, **`count`.** The table is UTF-8 (a BOM is dropped), parsed with Python's
  `csv` and the one-character `delimiter` (default `,`); the first row is the header and every row must have its
  number of cells. A name in two rows raises. `count` is the number of rows the table must have, so a changed
  table fails at open instead of shifting z.
- **Pairing.** The image array and the box arrays of one table have the same rows in the same order when they
  are bound with the same url, `image_url`, `name_column` and `delimiter`: plane k of each is row k.
- **Boxes** (`boxes_column`, with `boxes` and, for typed boxes, `classes`):
  - A box covers the pixels `[top, top + height) x [left, left + width)`: `top`/`left` 0-based, y down, x right.
    A fractional side is moved outwards (every pixel the box touches). A box that is empty, negative or reaches
    outside the PNG raises, naming the row.
  - **The "none" placeholder.** A cell whose only entry is `0, 0, 0, 0` (whatever its `type`) says "no object
    in this image" and gives an all-background plane, like an empty list `[]`. A `0,0,0,0` entry beside other
    boxes raises.
  - `boxes: instance`: each box is filled with its 1-based number in its cell; ids restart in every row
    (plane). `boxes: semantic`: each box is filled with the 1-based position of its `type` in `classes`, or
    with 1 when the column's boxes have no `type`.
  - `classes`: every `type` string the column uses, in class id order; types are compared after stripping
    spaces (`'g '` is `g`). Required when the boxes have a `type`, and a type that is not listed raises in both
    modes, so a class the record does not know is found rather than dropped. Refused for boxes without a
    `type`; a cell mixing boxes with and without raises.
  - Background is 0. Boxes are painted largest first, so where two overlap the smaller one is on top (cell
    order breaks ties). A box lying wholly under smaller ones leaves no pixel.
  - Cells are parsed with `ast.literal_eval` (literals only, nothing is evaluated); anything but a list of
    dicts with exactly these keys raises.
- **The labels are boxes, not masks**: where a box was drawn, not the cell's outline. Whether a binding may call
  that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **One shape.** Every image of one array must have the first row's shape and dtype. `open_array` reads the
  table and the first PNG's header (and, for boxes, parses the first cell); `read` checks every row it touches,
  an image's header before it is decoded and a box plane's PNG header before it is painted, and raises, naming
  the file, on another shape. There is no param to choose rows; a table over several image shapes needs a new
  snippet.
- **Access.** `plane`: an image plane costs its whole file (all of its `c`; the last decoded file is kept, so
  its three `c` planes decode once). A box plane costs the first range block of its PNG (1 MiB in the harness's
  `io`), which the image plane of the same crop reuses. Files are read through `io.open`; a server without
  Range raises `NeedsFetch` and the file is staged whole with `io.fetch`.
- **dtype.** The PNG's (`uint8` or `uint16`) for images, `uint16` for boxes. **Voxel size:** `None`: the table
  has none and a PNG `pHYs` chunk is a print size.

## Known limits

- PNG only, 8 or 16 bit, grey or colour. A palette or 1/2/4-bit PNG raises (the decoder would replace or
  rescale the stored values). Samples are exposed as stored, an alpha sample among them; a file whose decoded
  shape differs from its header (a `tRNS` chunk adds alpha) raises.
- Plain HTTP files only: no zip members, no globs, one table per array.
- The box keys are fixed (`top`, `left`, `height`, `width`, `type`); other box encodings (corner pairs, JSON
  with other names, one row per box) are not read. Other columns of the table (per-image classes, counts) are
  ignored.
- A cell longer than Python's `csv` field limit (131,072 characters) raises. More than 65,535 boxes in a cell
  raise.
- Shape and box checks beyond the first row happen at `read`, not at `open`.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour image yields single-sample
  crops.
- Each plane is at least one request to the file store. Zenodo limits requests per minute (it answered
  `429 TOO MANY REQUESTS` to the match agent's smoke runs) and the harness's `io` retries only so far.

## Example

`examples/zenodo-7484518-malaria-thick-smear-parasites.yaml`: 1,000 RGB brightfield fields (2056x2452, `uint8`
PNG, about 7.5 MB each) of Romanowsky-stained thick blood smears on Zenodo, and `data_base_malaria.csv`
(`;`-separated, one row per image) with a leukocyte box column and a typed parasite box column. Raw is `zcyx`
(1000, 3, 2056, 2452); the three labels are `zyx` (1000, 2056, 2452): leukocyte boxes by instance, parasite
boxes by instance and by class (1 trophozoite `t`, 2 schizont `e`, 3 gametocyte `g`). While testing, every row
of the table was rasterised from a local copy (12,712 leukocyte and 6,188 parasite boxes, the record's counts;
12 planes without a leukocyte and 298 without a parasite, all of them `0,0,0,0` placeholders; no box outside
its image), and on `c3_1.png` every one of the 23 leukocyte boxes holds stain-dark pixels while the same boxes
with top and left exchanged do not. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
