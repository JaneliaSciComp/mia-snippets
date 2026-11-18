# jpeg-yolo-boxes-glob-2

`jpeg-yolo-boxes-glob` plus one param, `images`: the binding names the JPEG of every box table, for files whose
URLs do not carry a name to pair them by. It supersedes that snippet: without `images` it is the same code and
reads every binding of it the same way (see *Testing*).

Reads a set of 2D files as one array, one file per z plane (`z` is kept for a single file too). The files of
one array are either

- **images**: 8-bit JPEG (`imagecodecs`, libjpeg). The samples are kept: an RGB file is three `c` planes
  (`zcyx`, c = R, G, B), a grey file none (`zyx`); or
- **YOLO box tables** (`params.boxes` set): text files with one row per box, `class x_center y_center width
  height`, the class a whole id from 0 and the four numbers fractions of the image's width and height. A table
  has no size of its own, so each file is rasterised (`zyx`, `uint16`) on the grid of **the JPEG it
  annotates**, whose header is read for it. A file without rows is an all-background plane.

`params.boxes` decides which of the two the urls are; the content is then checked (an image must start with the
JPEG marker, a table must parse), so a binding that mixes them up fails at open. `tiff-jpeg-voc-zip-glob` and
`jpeg-xml-boxes-numbers` read XML boxes, `jpeg-zip-glob` and `image-tiles-glob` images only.

- **URLs.** A plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>` or a
  glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch`; `*` also crosses `/`). Globs are
  expanded with `io.expand` and sorted **by stem** (the url without its extension), so `images/*.jpg` and
  `labels/*.txt` over the same stems pair plane for plane; several URLs are concatenated in binding order.
  The snippet cannot check the pairing across arrays; the binding's `reasons` say why the stems pair.
- **Which JPEG a table belongs to**, one of two ways:
  - **by name** (no `images`): `<folder>/labels/<stem>.txt` is read on the grid of `<folder>/images/<stem>.jpg`
    (the YOLO dataset layout); a table in a folder not named `labels` on that of `<stem>.jpg` beside it.
    `image_ext` names another extension than `jpg`. A table whose JPEG is missing raises.
  - **by the binding** (`images`): a list of urls, files or globs as the binding's `urls` are, expanded and
    ordered the same way; table k is read on the grid of JPEG k. This is for a repository that serves each file
    under an id, such as Mendeley Data (`.../files/<file id>/file_downloaded` for the tile and for its txt): no
    rule leads from one url to the other, so the binding says it, and its `reasons` say where the pairing
    comes from (a file listing with names). The paired raw is bound with the same urls in the same order as
    `images`, so plane k of the label lies on plane k of the raw.
    What is checked: `images` must stand for exactly as many files as the urls, no file twice, none of them
    among the urls; the first table must parse and the first JPEG must be one, at open; every table read is
    painted on its own JPEG's header shape, which must be the array's. What cannot be checked: that JPEG k
    *is* the picture table k was drawn on. Two tiles of one shape swapped in the list give boxes on the wrong
    tile, without an error. `images` and `image_ext` together raise.
- **One array per shape: `skip`.** An array has one shape, and every file must have the first file's. `skip`
  lists file stems (file name without its extension) to leave out of what the urls stand for; every stem must be
  exactly one file's, so a renamed or missing file raises instead of shifting z. The files left out are bound
  as another array, by glob or as plain member urls; give an image array and its table array the same list.
  With `images`, the JPEG at the position of a table left out is left out with it. Stems are taken from the
  urls, so `skip` is of no use for urls without file names (bind those files by listing the others).
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
  tables that exist, and the label array is then shorter than its raw, which smoke reports. With `images` too:
  every table needs a JPEG and a JPEG without a table cannot be listed. Bind such images as another raw.
- `images` is as right as the binding: the snippet cannot tell that a JPEG is the wrong one of the right shape.
- More than 65,535 boxes in a file raise.

## Testing

**The example binding has not been read on the real files by the job that wrote this snippet.**
`data.mendeley.com` answers each file URL with a redirect to
`prod-dcd-datasets-public-files-eu-west-1.s3.eu-west-1.amazonaws.com`, and that job was granted
`data.mendeley.com` only, so `harness smoke` on the example stopped at `host_not_allowed` with no byte read. A
job that reads this dataset needs both hosts. What was run instead, on the harness's local range server:

- a replica of the record's sample pair, behind the same kind of redirect from an id url without a file name:
  a 2400x2400 RGB JPEG and a table holding the record's row `0 0.055884 0.089157 0.051541 0.053387`. The raw
  opens as `zcyx` (1, 3, 2400, 2400) `uint8` and reads back as libjpeg decodes the file; the label as `zyx`
  (1, 2400, 2400) `uint16` with one box of id 1 over `y 149..279, x 72..196` and 0 elsewhere. The example
  binding, with its two URLs pointed at the replica, passes `smoke` and a 4-crop `crop` run. The real tile's
  shape and bit depth (JPEG, 2400x2400, 8 bit, 3 components) and the table's text are from the `harness
  describe` output quoted in the issue, not from a read of this snippet.
- `images`: globs and plain urls, order by position, `skip` leaving a table and its JPEG out together, and the
  refusals (count mismatch, a file twice, a file on both sides, tables and JPEGs swapped, `images` without
  `boxes` or with `image_ext`, a JPEG of another shape named at read).
- **same as `jpeg-yolo-boxes-glob`**: both snippets were opened on the same zips (deflated and stored members)
  with the same params, the YOLO layout, the beside layout with `image_ext`, `skip`, grey and RGB, instance and
  semantic, an empty table: equal axes, shape, dtype and every value, and the same errors with the same text (but the
  one that lists the box-table-only params, which now names `images` too).
  The real files of that snippet's own example (`zenodo-16945379-spider-mite-test-sets`) were not read again:
  `zenodo.org` is not among this job's hosts.

## Example

`examples/mendeley-457x9c7rp3-fossil-diatom-eucampia.yaml`: Mendeley Data 457x9c7rp3 (v3), 4,103 brightfield
RGB tiles of 2400x2400 px (NanoZoomer slide scans of Southern Ocean sediment, 230 nm per pixel by the paper) in
19 core folders and 4,004 YOLO tables with one class, *Eucampia antarctica*. The record names file URLs for one
tile and its table only, so the binding is that pair: the tile as raw, the table as an instance label with
`images` = the tile's url. The landing page is not a directory index; binding the rest needs the file ids from
the Mendeley API written into the record (see the binding's `reasons`). The binding's `snippets_commit` is the
commit before this snippet's own (a binding can't name the commit that adds it); a binding written by the
worker names a commit that contains the snippet.
