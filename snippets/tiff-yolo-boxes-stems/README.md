# tiff-yolo-boxes-stems

Reads a set of 2D files as one array, one file per z plane (`z` is kept for a single file). The files of one
array are either

- **images**: single-page TIFF, grey (`zyx`) or RGB with the samples kept as `c` (`zcyx`, c = R, G, B); or
- **YOLO box tables** (darknet / CVAT "YOLO 1.1" export): text, one row per box,
  `class_id x_center y_center width height`, the four numbers as fractions of the image width and height.
  Rasterised to `zyx`, `uint16`. No catalog snippet read YOLO tables before this one.

Which of the two is decided by the first file's leading bytes (TIFF magic or not), never by its name. An array
that mixes images and tables raises.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). Globs are expanded with `io.expand`, and **all files of the array together are sorted by
  stem** (the url without its last extension, then the extension), whatever the order of the URLs. So two image
  globs `dir/*.tiff` and `dir/*.tif` pair plane for plane with the one table glob `dir/*.txt` when they hold the
  same stems: `a.tiff` with `a.txt`, `b.tiff.tif` with `b.tiff.txt`. (Mind that `*.tif*` also matches
  `b.tiff.txt`.) The snippet cannot check the pairing across arrays; the binding's `reasons` must say why the
  stems pair.
- **`stems`** chooses the files of one array by name: the listed stems (file name without directory and last
  extension) are kept, in the sorted order above, and every other file the URLs stand for is left out. Every
  listed stem must be exactly one file's, so a missing or doubled file fails at open instead of shifting z. It
  is for sets whose images come in several shapes that neither name nor extension tells apart: an array has one
  shape, so each shape is one `raw` entry with that shape's stems, and its tables are label entries with the
  **same** list. The shapes are found by the binding's author (every TIFF header, once); the snippet does not
  look for them, because that would cost a request or more per file at every open.
- **Images.** Every file must be a single-series, single-page `YX` or `YXS` TIFF (not palette), and all files
  of one array must have the first file's shape and dtype; a file that differs raises at `read`, before its
  pixels are read, and names itself.
- **Tables** (`params`, both required for tables and refused for images):
  - The grid of a table is the image beside it: the one `.tif` / `.tiff` file in the same folder (same zip, or
    same HTTP directory, found with `io.list`) whose stem is the table's. Only its TIFF header is read. No such
    file, or two, raises.
  - `classes`: the class names in class id order (`obj.names`). A row whose class id has no name raises.
  - `boxes: instance`: each box is filled with its 1-based row number among the file's boxes (blank lines are
    not counted). Ids restart in every file (plane).
  - `boxes: semantic`: each box is filled with its class id + 1.
  - Background is 0; a table without rows is an all-background plane (an image annotated as empty).
  - A box spans `(x_center ± width / 2) * X` and `(y_center ± height / 2) * Y` pixels. Each edge is rounded to
    the nearest pixel boundary, so a pixel is painted when its centre lies in the box, and boxes written from
    whole-pixel rectangles with six decimals come back exactly. A box thinner than one pixel keeps the pixel
    its middle lies in.
  - Boxes are painted largest first, so where two overlap the smaller one is on top (file order breaks ties).
    An overlapped pixel carries one value; the other box's claim to it is lost.
  - A row that does not have five fields (a whole class id, four numbers), a box without area, or one that
    leaves the image by more than 1e-6 of its size (the rounding of the six decimals) raises and names its line.
- **The labels are boxes, not masks.** The array says where a box was drawn, not where the object's outline is.
  Whether a binding may call this `instance` or `semantic` is a human's decision (the record's encoding is
  `boxes-table`, for which the binding has no `segmentation_type`); see the example binding's `reasons`.
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or a Range-capable plain URL is
  read through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch`.
  `open_array` reads the first file only (an image's header; a table's bytes and its image's header). The last
  decoded file is kept, so reading its `c` planes in turn decodes it once.
- **dtype.** The image's own; `uint16` for tables. **Voxel size:** `None`; TIFF resolution tags describe print
  size and a YOLO table has none.

## Known limits

- One array per image shape. A set in which nearly every image has its own shape becomes many arrays, some of
  one file, and each array costs requests at open (below); nothing in this snippet pads or crops to a common
  grid, because a snippet is a pure reader.
- Requests: opening an image array costs the first file's zip local header and TIFF header (2 to 3 range
  requests), a table array the first table and its image's header (about 4, fewer when the blocks are already in
  the job's cache). A host with a request limit (Zenodo: 133 per minute, shared by everyone behind one address,
  answered with 429, which the harness does not retry) limits how many arrays one binding can hold.
- Shape and row checks beyond the first file happen at `read`, not at `open`.
- No multi-page, planar (`SYX`) or palette TIFF, no JPEG or PNG images, no pyramid levels or voxel size.
- YOLO variants with more than five fields per row (confidence, polygons, oriented boxes, track ids) raise.
- `obj.names`, `obj.data` and `train.txt` are not read; `classes` comes from the binding.
- More than 65,535 boxes in one file raise.

## Example

`examples/zenodo-12665681-siliceous-microfossils-yolo.yaml`: sediment-trap microfossils on microscope slides,
seven zips (all members stored) of grey `uint8` TIFFs with a CVAT YOLO export beside them
(`obj_train_data/<stem>.tiff` + `<stem>.txt`, `<stem>.tiff.tif` + `<stem>.tiff.txt`). The images are not of four
shapes, as the first files suggested, but of many: the 100 images of `test_dataset_part1.zip` alone have 25
shapes between 2032 and 2048 pixels a side (every header read while testing), mixed within the `.tiff` files.
The binding holds that zip's 4 shapes with five or more images (57 images) as 4 raws, each with its tables as
an `instance` and a `semantic` label; the `reasons` say what is left out and why. The zip's 100 tables hold
2,166 boxes of 12 of the 14 classes, all within the image; 34 tables are empty. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
