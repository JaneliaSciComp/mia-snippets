# jpeg-tile-class-csv-i290

Reads **one JPEG image per array**, and reads the **per-tile class table** of that image, a CSV beside it, as
a label array on the image's pixel grid. Written for whole-slide scans that were cut into square tiles and
classified tile by tile (MycorrhizaFinder's `<scan>_<timestamp>_cnn_1_annotations.csv`).

Two things no catalog snippet did before: a tile class table is read at all, and images whose height and width
differ are read without a shared canvas. `jpeg-zip-glob` raises on an image that differs from the first, and
`jpeg-zip-canvas-glob` pads every plane to the largest image, which is not workable when the scans are
hundreds to thousands of megapixels. Here each scan is its own array, so a binding lists one raw and one
label per scan.

- **URLs.** An image array takes one url, the JPEG: `<zip url>::<member>` or a plain URL, ending `.jpg` or
  `.jpeg`. A label array takes two, the same JPEG and its table (`.csv`); they are told apart by extension, and
  the table's url must be the image's url without its extension, then `_`, then anything
  (`.../X.jpg` and `.../X_2025-03-05T11_09_28.079853_cnn_1_annotations.csv`), so both are in one folder of
  one zip. No globs. When one image name is the start of another (`X.jpg`, `X_CROPPED.jpg`) that check cannot
  tell their tables apart: pair them in the binding by the table's full name pattern.
- **Image** (no `tile`). An 8-bit JPEG decoded by libjpeg (`imagecodecs`): a colour file is `cyx`
  (c = R, G, B), a grey file `yx`; `uint8`. Shape comes from the frame header; a file that decodes to another
  shape raises at `read`. EXIF orientation is not applied.
- **Tile labels** (`tile` set; `classes` is then required, and both `classes` and `ignore` are refused
  without `tile`). `yx`, `uint8`, the image's height and width.
  - The table is a CSV with a header (UTF-8, a BOM is accepted) naming `row`, `col` and one column per
    class, in any order. Each data row is one tile; `row` and `col` are whole numbers from 0 (`84` or
    `84.0`), a class cell is 0 or 1 (`1`, `1.0`).
  - Tile (row, col) is the pixels of rows `[row * tile, (row + 1) * tile)` and columns
    `[col * tile, (col + 1) * tile)`: tiles are counted from the top left corner of the decoded image, `row`
    down and `col` to the right, without gaps or overlap. The strip right of and below the last whole tile
    belongs to no tile.
  - A tile's pixels hold the 1-based position in `classes` of the class column that is 1 in its row.
    **0 means no class is recorded**: a tile without a row, a row with no class set, or the strip beyond the
    last whole tile. A class such as `Background` therefore has its own nonzero id.
  - Checked at open, on the whole table: the header has `row`, `col` and every name in `classes` once; every
    other column is in `ignore`; every row has the header's number of fields; `row` and `col` are whole and
    the tile lies inside the image; no tile has two rows; class cells are 0 or 1; **at most one class is set
    per row** (several raise, naming the line: the snippet does not choose).
- **Access.** An image is `plane`: any region costs fetching the file (`io.fetch`; a zip member comes inflated
  and CRC-checked) and decoding it whole, 3 bytes a pixel. The last decoded image is kept, one for all arrays
  of the snippet, so the three `c` planes of a scan decode it once and a binding with hundreds of scans holds
  one of them in memory. A label region is made from the table's tile grid: nothing is fetched or decoded
  after open, and only the region asked for is built. It still reports `plane`, like its image, because the
  harness then searches the whole label for annotated tiles and places every other crop on one; with `chunk`
  it tests only the box it drew, and where a table annotates a few percent of a scan (the ErM tables of the
  example's record: 1.5% of the tiles) every crop came out without a label.
- **Open.** The frame header is read from the image's first bytes: by range for a plain URL, a stored member
  or a file staged before; a deflated member is inflated from its start through the zip (about 1 MiB of
  network per image, not the image). Labels also fetch the table.
- **Voxel size.** `None`: a JPEG's JFIF density is a print size, and the table has none.

## Choosing `tile`

The table gives tile indices, not pixels, and neither it nor the JPEG states the tile edge. Take it from the
software that wrote the table. When that cannot be read, it can be measured from the data, as was done for the
example binding (its `reasons` give the numbers):

1. **Upper bound.** Every annotated tile must lie inside its image, so over all tables
   `tile <= min(height / (max row + 1), width / (max col + 1))`. The snippet enforces this: a larger `tile`
   raises at open.
2. **Exact value.** A table that annotates a complete rectangle from tile (0, 0) covers the whole grid, so its
   row and column counts are `floor(height / tile)` and `floor(width / tile)`. With images of many sizes only
   one whole number satisfies this for all such tables.
3. **Check on pixels.** Decode one scan and compare the tiles of a visually distinct class (background against
   tissue) with the image under candidate tile sizes and origins; the right grid separates them best.

Do step 3 on more than one scan, and on scans of every kind in the dataset: in the example's record the
tables of one model fit 126-pixel tiles on one scan and no single size on another, and step 1 cannot tell.
A `tile` that is too small still opens (every tile fits) and puts the labels in the wrong place, so step 1
alone is not enough. Scans of one dataset may use different tiles.

## Known limits

- One image per array: no z stack, no glob urls. A dataset is one binding entry per image.
- The column names `row` and `col` are fixed; square tiles from the top left corner only, no offset, overlap
  or stride.
- One class per tile. A table with multi-class rows needs a decision this snippet does not take.
- At most 255 classes (`uint8` ids).
- A raw read decodes the whole scan into memory (3 bytes a pixel; 2.3 GB for the largest scan of the example, 6.5 GB for a 33519x64554 scan), and the
  harness keeps the plane it read last while the next is decoded. To place a crop on an annotation the harness
  also reads the label of the whole scan (1 byte a pixel) and builds masks of the same size.
- No 12-bit or CMYK JPEG, no `samples` selection.
- The frame-header read of a deflated zip member goes through the zip's directory, so opening such an array
  needs the zip's host (or the zip staged whole); the member alone, staged by `harness fetch`, serves `read`
  and, in a job without the host, the header too.

## Example

`examples/zenodo-17476686-mycorrhizafinder-root-tiles.yaml`: the 55 micrograph scans of stained roots in
`am.zip` on Zenodo, each bound as one raw (`cyx`, 70 to 771 megapixels) and one semantic label of six classes
on 252-pixel tiles. The record's other seven zips (ErM scans) are read by the snippet but not bound: their tile
size is not one number (the binding's `reasons` say what was measured).
