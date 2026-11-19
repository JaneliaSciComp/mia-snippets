# png-yolo-zip-glob

Reads a set of 2D files as one array, one file per z plane (`z` is dropped for a single file). The files of
one array are either

- **PNG images** (`imagecodecs`), 8 or 16 bit. The samples are kept: an RGB file is three `c` planes (`zcyx`,
  c = R, G, B), a grey file none (`zyx`); or
- **YOLO box annotations**, one text file per image, rasterised on the grid of the PNG beside each (`zyx`,
  `uint16`). No catalog snippet read YOLO text before this one; `tiff-jpeg-voc-zip-glob` and
  `jpeg-xml-boxes-numbers` read VOC-like XML.

Which of the two is decided by the first file's leading bytes (the PNG signature, or not), never by its name.
An array that mixes images and annotations raises.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and its files are sorted **by stem** (the url without
  its extension, then the extension); several URLs are concatenated in binding order. Sorting by stem is what
  makes an image glob (`train/*.png`) and the annotation glob beside it (`train/*.txt`) pair plane for plane
  when the two hold the same stems: sorted by name, `a.png`, `a.q.png` would meet `a.q.txt`, `a.txt`. The
  snippet cannot check across arrays that the two globs hold the same stems, so the binding's `reasons` must
  say why they do. Bind the images with this snippet too, so both sides have the same order.
- **Images.** Every file must be a PNG with 8 or 16 bits per sample and no palette, and all files of one array
  must have the first file's shape and dtype; a file that differs raises at `read` and names itself. Files of
  another shape are bound as another array. Shape and dtype come from the IHDR chunk, so `open_array` decodes
  nothing. A palette or 1/2/4-bit PNG raises (the decoder would turn the stored indices into colours or
  rescale them), as does a file whose decoded shape is not its IHDR's (a `tRNS` chunk).
- **YOLO files.** Text rows `<class> <x_center> <y_center> <width> <height>`, separated by tabs or spaces:
  an integer class id from 0, then the box centre and size as fractions of the image width (x) and height (y).
  A CR before the newline, blank lines and a UTF-8 BOM are ignored. **A file with no rows is an all-background
  plane** (an image without objects).
  - **The grid.** A YOLO file states no image size, so each file's grid is read from the IHDR of the PNG with
    the same stem beside it: the annotation's url with its extension replaced by `.png`. That PNG must exist,
    and its (height, width) must be the first file's.
  - A box covers the pixels `[y0, y1) x [x0, x1)` with `x0 = round((x_center - width/2) * X)`,
    `x1 = round((x_center + width/2) * X)` and the same in y: its edges are moved to the nearest pixel boundary.
    Annotation tools write integer pixel rectangles divided by the image size, and rounding gives those
    integers back (in the example dataset 17,468 of the 17,480 edges are within 0.001 pixel of a whole
    number). An edge past the image is clipped to it.
  - A row of width 0 or height 0 is a point or a line, not a box: it paints nothing, and keeps its row number
    (so instance ids can skip a number).
  - `classes` (required): the class names in YOLO id order (id 0 first). Only their number is used for the
    values; a row whose class id has no name raises, in both modes, so a class the record does not know is
    found rather than dropped.
  - `boxes: instance`: each box is filled with its 1-based row number in its file (blank lines do not count).
    Ids restart in every file (plane).
  - `boxes: semantic`: each box is filled with its class id + 1 (the 1-based position of its name in `classes`).
  - Background is 0. Boxes are painted largest first, so where two overlap the smaller one is on top (file
    order breaks ties). An overlapped pixel carries one value; the other box's claim to it is lost.
  - Raises: a row that does not have five fields (YOLO polygon rows of the segmentation format and
    oriented-box rows have more), a class id that is not a plain non-negative integer, a number that is not
    plain decimal or exponent notation (`nan`, `inf`), a centre or a size outside 0..1 (pixel coordinates
    instead of fractions), a box of positive size that covers no pixel after rounding, non-ASCII bytes, more
    than 65,535 rows in one file.
  - `boxes` and `classes` are both required for YOLO files and refused for images.
- **The labels are boxes, not masks.** The array says where a box was drawn, not where the object's outline is.
  Whether a binding may call this `instance` or `semantic` is a human's decision (the record's encoding is
  `boxes-table`, for which the binding has no `segmentation_type`); see the example binding's `reasons`.
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or a Range-capable plain URL is
  read through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch`.
  `open_array` reads the first file's first 26 bytes (an image) or the first annotation file and the first 26
  bytes of its PNG. The last decoded file is kept, so reading its three `c` planes in turn decodes it once.
- **dtype.** `uint8` or `uint16` for images, by the PNG's bit depth; `uint16` for boxes. **Voxel size:**
  `None`: YOLO text has none and a PNG's `pHYs` chunk describes print size.

## Known limits

- A label plane needs the header of its PNG. When the members are deflated (the example dataset) that means
  staging the whole PNG (`io.fetch`, kept in the job's cache, so it is paid once when the raw plane is read
  too): reading a label plane alone costs its image's bytes.
- Over HTTP each deflated member costs two requests (local header, compressed bytes), made lazily per plane.
- The image beside an annotation is `<stem>.png` in the same folder. YOLO layouts with `images/` and `labels/`
  folders, or JPEG images, are not read.
- Shape, class and row checks beyond the first file happen at `read`, not at `open`: opening every member of a
  deflated set would fetch all of it.
- An image without a `.txt` is not an empty plane: the annotation glob then has fewer files than the image
  glob and the harness refuses the pair. Every image needs its (possibly empty) file.
- No palette, 1/2/4-bit or interlace-specific handling beyond what `imagecodecs.png_decode` does; no JPEG or
  TIFF (see `image-tiles-glob`, `tiff-jpeg-voc-zip-glob`); no YOLO polygons, oriented boxes, keypoints or
  confidence columns; `dataset.yaml` is not read (the class names come from the binding).

## Example

`examples/zenodo-11126980-tricellular-liver-spheroids-yolo.yaml`: a 4.0 GB zip (all members deflated) of
brightfield images of tri-cellular liver spheroids, 516 RGB PNGs of 2456x2054 (y, x) `uint8` in `train/` (413),
`val/` (51) and `test/` (52), each with a YOLO `.txt` of the same stem (tab-separated, CRLF line ends; classes
0 Healthy, 1 Unhealthy from the zip's `dataset.yaml`). Each folder is bound as one `zcyx` raw and its
annotations twice: `boxes: instance` and `boxes: semantic`. All 516 annotation files were read and painted
while testing: 4,370 rows (4,176 Healthy, 194 Unhealthy, at most 20 per file), no other class id and no box
hidden completely by others. Three things a reader of YOLO text meets here: one file is empty
(`train/1C43C05C82D872064DDB806197D5532D.txt`, 0 bytes, an all-background plane); one row has width and
height 0 (`test/9484472A1D68087DAF021788B7E9BEE1.txt`, painted as nothing); and 10 boxes in `train/` reach 1
or 2 pixels past the left or top image edge and are clipped. The example binding's `snippets_commit` is the
commit before this snippet's own (a binding can't name the commit that adds it); a binding written by the
worker names a commit that contains the snippet.

zenodo.org answers `429 TOO MANY REQUESTS` to bursts of requests (seen while testing, when many members
were fetched side by side); a job that reads many planes of this dataset has to pace itself, which is the
harness's side. `io.fetch` of the empty stored member is answered `416` (a zero-length range), so the snippet
reads through `io.open` first, which returns its zero bytes without a request.
