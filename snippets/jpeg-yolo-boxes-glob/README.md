# jpeg-yolo-boxes-glob

Reads a set of 2D files as one array, one file per z plane (`z` is kept for a single file too). The files of
one array are either

- **images**: 8-bit JPEG (`imagecodecs`, libjpeg). The samples are kept: an RGB file is three `c` planes
  (`zcyx`, c = R, G, B), a grey file none (`zyx`); or
- **YOLO box tables** (`params.boxes` set): text files with one row per box, `class x_center y_center width
  height`, the class a whole id from 0 and the four numbers fractions of the image's width and height. A table
  has no size of its own, so each file is rasterised (`zyx`, `uint16`) on the grid of **the JPEG it
  annotates**, whose header is read for it. A file without rows is an all-background plane.

`params.boxes` decides which of the two the urls are; the content is then checked (an image must start with the
JPEG marker, a table must parse), so a binding that mixes them up fails at open. No catalog snippet read YOLO
tables before this one: `tiff-jpeg-voc-zip-glob` and `jpeg-xml-boxes-numbers` read XML boxes, `jpeg-zip-glob`
images only.

- **URLs.** A plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>` or a
  glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch`; `*` also crosses `/`). Globs are
  expanded with `io.expand` and sorted **by stem** (the url without its extension), so `images/*.jpg` and
  `labels/*.txt` over the same stems pair plane for plane; several URLs are concatenated in binding order.
  The snippet cannot check the pairing across arrays; the binding's `reasons` say why the stems pair.
- **Which JPEG a table belongs to.** `<folder>/labels/<stem>.txt` is read on the grid of
  `<folder>/images/<stem>.jpg` (the YOLO dataset layout); a table in a folder not named `labels` on that of
  `<stem>.jpg` beside it. `image_ext` names another extension than `jpg`. A table whose JPEG is missing raises.
- **One array per shape: `skip`.** An array has one shape, and every file must have the first file's. `skip`
  lists file stems (file name without its extension) to leave out of what the urls stand for; every stem must be
  exactly one file's, so a renamed or missing file raises instead of shifting z. The files left out are bound
  as another array, by glob or as plain member urls; give an image array and its table array the same list.
  **Why the binding has to say it:** shapes are in the JPEG headers only, and reading every header of a
  deflated set at open means fetching all of it. The list is checked where it is cheap: `open` reads the first
  file's header, and `read` checks every file it touches (an image's header before it is decoded; a table's
  JPEG header) and raises, naming the file, on another shape. A wrong list cannot produce a mis-shaped or
  shifted plane, only an error.
- **Boxes** (`boxes` and `classes`: both required for tables, `classes` refused for images):
  - `classes`: the class names in YOLO class id order, as the dataset's `data.yaml` `names` lists them. The
    table stores ids only, so the names document the ids and bound them: a row whose id has no name raises.
  - `boxes: instance`: each box is filled with its 1-based row number in its file (blank lines are not
    counted); ids restart in every file (plane). `boxes: semantic`: each box is filled with its class id + 1.
  - Background is 0. A box covers the pixels `[y0, y1) x [x0, x1)` with `x0, x1 = (x_center -+ width / 2) *
    image width` and y likewise. An edge within 1e-6 px of a pixel edge is that edge (boxes drawn on whole
    pixels come back from the fractions with float noise); any other edge is moved outwards (near edge floored,
    far edge ceiled): every pixel the box touches.
  - A box that is empty or reaches outside the image raises, as does a row that is not exactly a whole class
    id and four finite numbers (YOLO polygon rows, oriented boxes, rows with a confidence), naming file and
    line. Nothing is clipped or skipped.
  - Boxes are painted largest first, so where two overlap the smaller one is on top (row order breaks ties).
    An overlapped pixel carries one value; the other box's claim to it is lost.
  - **Exif orientation.** The JPEG is returned as stored; an Exif `Orientation` is not applied. A table *with*
    rows whose JPEG has an orientation other than 1 raises: its stored pixels are turned against the displayed
    picture, and the fractions do not say which of the two they were measured on.
- **The labels are boxes, not masks**: where a box was drawn, not the object's outline. Whether a binding may
  call that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or Range-capable URL is read
  through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch`. `open_array`
  reads the first file (tables: the first table and its JPEG's header), decoding nothing. A JPEG header is
  taken from the file's first 64 KiB, and from the whole file only when its start-of-frame segment lies
  further in. Reading a table plane also gets its JPEG (whole, when it is a deflated member); `io` keeps it, so
  the raw plane of the same crop costs no more. The last decoded file is kept, so its three `c` planes decode
  once.
- **dtype.** `uint8` for images, `uint16` for boxes. **Voxel size:** `None` (JPEG densities are print sizes,
  and a table has none).

## Known limits

- The shape groups are not discovered: someone has to read every JPEG header once and write `skip`.
- Shape, row and class checks beyond the first file happen at `read`, not at `open`.
- JPEG only (8-bit, 1 or 3 components); no TIFF or PNG, no CMYK, no 12-bit.
- Five-field detection rows only: no YOLO segmentation polygons, oriented boxes, keypoints or confidences.
  `data.yaml` is not read; the binding carries the class names.
- An image without a table (YOLO's way to say "no objects") is not given a plane: a table glob stands for the
  tables that exist, and the label array is then shorter than its raw, which smoke reports.
- More than 65,535 boxes in a file raise.
- On real data only deflated zip members were read; stored members, plain URLs and the beside-the-table layout
  go through the same code and were exercised on a local range server only.

## Example

`examples/zenodo-16945379-spider-mite-test-sets.yaml`: one 4.0 GB zip at Zenodo (1455 files, all deflated)
with three folders, `v209_five_class`, `v210_three_class` and `v211_four_class`, each holding the same 242
focus-stacked RGB photographs of two-spotted spider mites and eggs on leaves under `images/` and one YOLO table
per image under `labels/`, relabelled per class set (4449, 4298 and 4369 boxes). 239 of the images are
5502x8254x3, two are 5500x8254x3 and one is 5500x8252x3, so each folder is three `zcyx` raws (`skip` on the
glob for the 239, plain member urls for the others), and each raw has its tables twice, `boxes: instance` and
`boxes: semantic`: 9 raws, 18 labels. While testing, the headers of all 242 JPEGs were read, every plane of the
18 label arrays was painted from the real tables (no row refused, no box outside its image, none hidden
completely), smoke and a 4-crop run passed on the real zip, and the overlay of a crop shows a box drawn round
an egg. The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name
the commit that adds it); a binding written by the worker names a commit that contains the snippet.
