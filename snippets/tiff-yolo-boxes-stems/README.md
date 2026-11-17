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
- **Access.** `plane`: one file is one plane (all of its `c`). `open_array` reads the first file only (an image's
  header; a table's rows and its image's header); `read` gets the others. The last decoded file is kept, so
  reading its `c` planes in turn decodes it once.
- **Few requests.** The way a file is asked for is chosen to cost a rate-limited host as few requests as it can:
  - An image (a file named `.tif` / `.tiff`) is staged whole with `io.fetch`: two requests the first time (the
    zip member's local header and its data; an image is decoded whole anyway), none after that for any array of
    the job or of a later job with the same dataset cache. So the header a table asks for and the pixels its raw
    array reads are the same two requests, and a staged image is read offline.
  - A table (any other name) is read by range with `io.open`: the member's local header every time it is opened,
    its bytes once per job. It is not staged, because `io.fetch` cannot ask for an empty stored member (the range
    of no bytes is answered with 416) and tables without rows are common. A deflated member (`NeedsFetch`) is
    staged. A file with another name that begins as a TIFF is then staged as an image; what a file is, is still
    decided by its bytes.
  - Counted on the example: opening its 4 raws and 8 labels costs 21 requests (about twice that when images
    were read by range too), and a new plane read as raw, instance and semantic label 4 or 5 (2 for the image,
    1 for each table open, 1 more when the table's bytes are not in a cached block yet).
- **Waiting on 429.** A step that makes requests (a glob's expansion, a listing, one file) and is answered with
  HTTP 429 *Too Many Requests* is tried again, up to 6 times: after the answer's `Retry-After` seconds + 1,
  not less than 10 s and not more than 90 s (10 s when the header names no seconds). The harness does not wait
  on 429 itself; it does ask again after a dropped connection or a 5xx, so the snippet leaves those to it. The
  error is told by class name and status, as a snippet may not import the HTTP client. Every other error
  (`BudgetExceeded`, `HostNotAllowed`, a 404, the snippet's own) is raised at once. A read that meets a spent
  limit therefore takes up to minutes longer instead of failing; a limit that does not lift within about six
  minutes still fails with the 429.
- **dtype.** The image's own; `uint16` for tables. **Voxel size:** `None`; TIFF resolution tags describe print
  size and a YOLO table has none.

## Known limits

- One array per image shape. A set in which nearly every image has its own shape becomes many arrays, some of
  one file, and each array costs requests at open (below); nothing in this snippet pads or crops to a common
  grid, because a snippet is a pure reader.
- Requests: opening an image array costs 2 (its first image, staged), a table array 2 (its first table; the
  image is its raw's first, already staged) or 1 (the same table again, for the other `boxes` mode). A host with
  a request limit (Zenodo: `x-ratelimit-limit: 133` a minute, shared by everyone behind one address) is waited
  for, so the limit costs time, not the read; a binding of hundreds of arrays opens over minutes.
- Bytes: a table's grid costs its whole image (some MB here) where a header would do. That is free when the raw
  is read too, as in every crop or stage of a paired label, and wasted only by a reader of labels alone. A table
  costs the 1 MiB cache block (or two) it lies in.
- A stored member named `.tif` / `.tiff` that is empty cannot be staged (416); it is no image either way.
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
