# csv-box-rows-tiff-grid

Reads the bounding boxes **one image** has in a CSV table that holds **one row per box** for many images (an
EXACT / SlideRunner annotation export, e.g. `SDATA_Final_Annotations.csv` of the P-Hem hemosiderophage dataset),
and rasterises them on the pixel grid of that image's TIFF: an Aperio SVS whole-slide image, whose first page
is the full-resolution level. The array is `yx`, the shape of that page.

It exists because `png-csv-boxes-table` reads another table: one row per image with a list of
`{top, left, height, width}` boxes, a plain url only, and PNG grids. Here a row is a box, the box is a corner
dict, the table is a zip member, the rows of one slide have to be picked out of all slides', and the grid is an
SVS.

- **urls: two, in this order.** First the table: a plain url or a `<zip>::<member>` (a deflated member is
  fetched, the member only, not the zip). Then the slide the boxes are on, the paired raw's url. Of the slide
  only the first IFD is read (one or two range blocks), for the shape and the pixel size; no pixel of it is
  decoded. A url that does not start as a TIFF in second place, or does in first place, raises. No globs.
- **`image_name`, `name_column`.** The rows whose `name_column` cell equals `image_name`, compared exactly as
  written, are the slide's boxes. The name is the table's own, which need not be the file name the store gives
  the slide; the binding says which name goes with the slide's url. No row with that name is an error, not an
  empty label: a slide without boxes cannot be told from a wrong name.
- **`boxes_column`.** Each row's cell is a Python-literal dict with exactly the keys `x1`, `x2`, `y1`, `y2`,
  in pixels of the full-resolution level, x right and y down from the image corner. The box covers the pixels
  `[y1, y2) x [x1, x2)`; a fractional corner is moved outwards (every pixel the box touches). Cells are parsed
  with `ast.literal_eval` (literals only, nothing is evaluated), and only the rows of this image are parsed.
  A cell that is anything else, or a box that is empty, negative or reaches outside the slide's page, raises,
  naming the table row: a box off the grid is the only sign of a wrong `image_name` / slide pairing the
  reader can see (slides of one size pass it, so the binding has to check the pairing on the pixels).
- **`boxes: instance`** (`uint32`): each box is filled with its 1-based number among the image's rows, in table
  order. **`boxes: semantic`** (`uint8`): each box is filled with the 1-based position of its `class_column`
  cell in `classes`. `classes` lists every value the column takes as the table writes it (strings or whole
  numbers, compared as stripped text), in class id order; ids start at 1 because 0 is background, so a table
  whose grades are 0..4 gives ids 1..5. A row whose class is not listed raises. `class_column` and `classes`
  are refused with `instance`.
- **Overlap.** Background is 0. Boxes are painted largest first, so where two overlap the smaller one is on top
  (table order breaks ties: the later row is on top). A box lying wholly under smaller ones leaves no pixel.
- **`delimiter`** (default `,`), **`count`.** The table is UTF-8 (a BOM is dropped), parsed with Python's `csv`
  and the one-character delimiter; the first row is the header and every row must have its number of cells
  (quoted cells may hold line breaks). `count` is the number of rows the image must have, so a changed table
  fails at open instead of renumbering the instance ids.
- **The labels are boxes, not masks**: where a box was drawn, not the cell's outline. Whether a binding may
  call that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Access** `chunk`: the table is read whole at open (and the slide's header); a read paints only the boxes
  its region touches and costs no bytes. **Voxel size**: the slide's Aperio `MPP` when its description has
  one (the same value `wsi-czi-svs-rgb` reports for the raw), else `None`.

## Known limits

- The corner keys are fixed (`x1`, `x2`, `y1`, `y2`); other encodings (`top/left/height/width`, lists of boxes
  per row, polygons) are not read. Other columns are ignored.
- One image per array. Every slide is its own raw and label pair, each label opening the whole table again
  (from the cache after the first).
- A broken cell is an error, never a skipped box. In `SDATA_Final_Annotations.csv` 61 of the 297,383 rows, in
  19 of the 74 slides, hold a truncated pandas Series printout instead of a dict (`unique_identifier\n<id>
  {'x1': 10178, 'x2': 10278, 'y1': 4167, 'y2': 4...`): their boxes are not in the table, so those 19 slides
  raise at open and need a human decision (drop the rows, or read the `.sqlite` beside the table) and a new
  snippet. The other 55 slides, the example's among them, have none.
- A slide with no rows cannot be bound as an all-background label.
- The grid is the first page of the TIFF; a file whose full-resolution image is another page is not handled.
  The page's shape is the only check that the slide is the one the rows are for (every box must lie inside).
- A cell longer than Python's `csv` field limit (131,072 characters) raises.

## Example

`examples/figshare-c-5486799-p-hem-hemosiderophage-cytology-wsi.yaml`: one human bronchoalveolar lavage slide
(`10835_19 humane BAL Berliner Blau.svs`, 26999 x 28047, Prussian blue) from the Figshare collection as the
raw (`wsi-czi-svs-rgb`), and its 4,553 hemosiderophage boxes out of the 297,383 rows of the `;`-separated
`SDATA_Final_Annotations.csv` (a 46 MB deflated member of the Zenodo code release, 11.7 MB fetched) twice: by
instance, and by hemosiderin grade (ids 1..5 = grades 0..4). While testing, the boxes were laid over the slide:
they frame the macrophages, the grade colours follow how blue the cell is, and the same boxes with x and y
exchanged or flipped in y hold much less stain. The example binding's `snippets_commit` is the commit before
this snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a
commit that contains the snippet.
