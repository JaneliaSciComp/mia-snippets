# jpeg-yolo-boxes

Reads a set of JPEG photographs as one array, one image per z plane (`z` is kept for a single image too), or the
**YOLO box tables** that belong to them as a label array on the same grid. The array is either

- **images**: 8-bit JPEG (`imagecodecs`, libjpeg). The samples are kept: an RGB file is three `c` planes
  (`zcyx`, c = R, G, B), a grey file none (`zyx`); or
- **box tables**: one text file per image, one row per box, `class x_center y_center width height`, the class
  an integer id from 0 and the four numbers fractions of the image width and height (the YOLO detection
  format). Each table is rasterised (`zyx`, `uint16`) on the grid of **the JPEG with the same file name stem**
  (`labels/<stem>.txt` -> `images/<stem>.JPG`, any folder, any extension), whose header is read for it. An
  empty table is an all-background plane.

It was written for a dataset published as several zip parts in which an image and its table are often in
different parts. The catalog's other box snippets (`jpeg-xml-boxes-numbers`, `tiff-jpeg-voc-zip-glob`) read XML
with pixel corners; `jpeg-zip-glob` reads images only.

- **URLs.** A plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>` or a
  glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch`; `*` also crosses `/`), expanded with
  `io.expand`. A url whose file name ends in `.txt` is a box table; every other url is an image.
  - **Raw:** the image urls. z is binding order, and name order within a glob.
  - **Labels:** *the image urls of the raw, in the raw's order*, and the urls of the tables (anywhere in the
    list). z follows the images, never the tables: plane k is the table of image k, found by stem. Open fails
    unless every image has exactly one table and every table an image (a stray `classes.txt` matched by a wide
    glob raises), and when two images share a stem. Anchor globs at their folder (`…/images/*.JPG`,
    `…/labels/*.txt`).
- **`count`**: the number of images the array must have; open fails on another number. Give raw and labels
  the same.
- **Boxes** (`boxes` and `classes`: required for a label array, refused for a raw):
  - `classes`: the class names in class id order, the first is id 0. The tables hold ids only; the list says
    what they mean and how many there are. A row whose id is past the list raises.
  - `boxes: instance`: each box is filled with its 1-based row number in its table; ids restart in every
    plane. `boxes: semantic`: each box is filled with class id + 1 (its position in `classes`, from 1).
  - Background is 0. A box covers every pixel it touches: its edges `(centre ± size/2) * image size` are moved
    outwards to pixel boundaries (an edge within 1e-6 px of a boundary is on it, since the fractions are
    rounded decimals). A box that is empty or reaches outside the image raises.
  - Boxes are painted largest first, so where two overlap the smaller one is on top (file order breaks ties).
  - A row that is not five fields (a YOLO polygon row has more), a class that is not a whole number, or a
    number that is not finite raises, naming file and line.
- **Exif orientation, `drawn_on`.** Images are returned **as stored**; an Exif `Orientation` is never applied to
  pixels. Phone photographs are often stored turned (orientation 6: the stored 3024x4032 is shown 3024 wide,
  4032 high), and a box table does not say whether its fractions are of the stored grid or of the picture a
  viewer shows. `drawn_on: displayed` says the latter (what a browser-based tool such as Label Studio shows,
  and what YOLO training code that applies the orientation expects): each box is turned back onto the stored
  pixels by its own image's orientation (all of 1..8; width and height swap for 5..8), so it lies on the raw as
  this snippet returns it. `drawn_on: stored` uses the fractions on the stored grid as they are. Without the
  param a table with boxes whose JPEG has an orientation other than 1 raises. An image without the tag, or
  with 1, is the same either way. **Look before choosing**: draw the boxes both ways on one image.
- **The labels are boxes, not masks**: where a box was drawn, not the cell's outline. Whether a binding may call
  that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or Range-capable URL is read
  through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch` (one request).
  `open_array` reads the listings, the first image (header only, nothing decoded) and, for labels, the first
  table. Reading a label plane gets its table and its JPEG (for shape and orientation); `io` keeps the JPEG,
  so the raw plane of the same crop costs no more. The last decoded plane is kept, so its three `c` planes
  decode once.
- **dtype.** `uint8` for images, `uint16` for boxes. **Voxel size:** `None` (JPEG densities are print sizes).

## Known limits

- All images of one array must have the first image's stored shape; another shape raises at `read`, naming the
  file (images and labels alike). Shapes, orientations and tables beyond the first are checked at `read`, not
  at open.
- An image without a table raises at open; the YOLO habit of leaving out the table of an image without objects
  is not read as background (an empty table is).
- YOLO polygon (segmentation), oriented-box and pose rows are not read. More than 65,535 boxes in a table raise.
- JPEG only (8-bit, 1 or 3 components); no PNG or TIFF, no CMYK, no 12-bit.
- Overlapping boxes hide each other (smaller on top); the table's full extents are not recoverable from a plane.
- Tested on real data on deflated zip members only, and on Exif orientation 6 only; stored members, plain
  URLs and the other orientations were exercised on synthetic files over a local range server.

## Example

`examples/zenodo-19692572-malaria-thin-smear-yolov8.yaml`: 4,496 phone photographs (3024x4032 RGB JPEG, Exif
orientation 6) of Giemsa-stained thin blood films in six Zenodo zip parts (11.3 GB, all members deflated), with
one Label Studio YOLO table each in parts -001 to -005; only 1,445 pairs share a part. One `zcyx` raw
`(4496, 3, 3024, 4032)` and its tables twice, `boxes: instance` and `boxes: semantic`, six classes,
`drawn_on: displayed`. While testing, all 4,496 tables were read and painted from a copy of the label members
(45,366 boxes, class ids 0..5, 1-166 per image, none empty, malformed or outside its image); 8 planes spread
over the six parts were read through the snippet (each with orientation 6, the table of the image's own stem,
every box id present); and the boxes were drawn on two images: turned as `displayed` they surround the
cells, as `stored` (one image) they lie on background. Many images are annotated sparsely (a few boxes among hundreds of
cells), so background is "not boxed", not "no cell". The example binding's `snippets_commit` is the commit
before this snippet's own (a binding can't name the commit that adds it); a binding written by the worker names
a commit that contains the snippet.
