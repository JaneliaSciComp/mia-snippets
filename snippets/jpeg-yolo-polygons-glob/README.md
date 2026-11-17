# jpeg-yolo-polygons-glob

Reads a YOLO segmentation set kept in a zip as two arrays on one grid, one file per z plane (`z` is kept for a
single file too). The files of one array are either

- **images**: 8-bit JPEG (`imagecodecs`, libjpeg). The samples are kept: an RGB file is three `c` planes
  (`zcyx`, c = R, G, B), a grey file none (`zyx`); or
- **YOLO segmentation label files** (the label form, chosen by setting `polygons`): text, one row per object,
  `class x1 y1 x2 y2 ...`, the vertices of one polygon as fractions of the image's width and height. Each file
  is rasterised (`zyx`, `uint16`) on the grid of **the JPEG with the same stem**, whose header is read for it.
  An empty file (or one of blank lines) is an all-background plane.

It was written because the catalog's other annotation rasterisers for JPEG sets (`jpeg-xml-boxes-numbers`,
`tiff-jpeg-voc-zip-glob`) parse XML boxes, and `jpeg-zip-glob` reads images only.

- **URLs.** A zip member `<zip url>::<member>` or a zip member glob `<zip url>::<pattern>` (`fnmatch`; `*`
  also crosses `/`), expanded with `io.expand`: sorted by member name, several urls in binding order. Anchor
  the pattern at the folder (`set/images/*.jpg`, `set/labels/*.txt`).
- **Pairing: `images`** (required for labels). The label planes are not in the label files' own order: they
  are **the images `images` matches, in name order, each with the label file of its stem** (the name without
  folder and extension, so `images/a.jpg` goes with `labels/a.txt`). `images` is a member pattern over the
  zip the label urls name; give it the member part of the image array's url, and z index k of the label is
  then the annotation of z index k of the image array whatever the two extensions sort like. Open raises
  unless images and label files pair one to one: an image without a label file, a label file without an
  image, or two files of one stem.
- **Grid.** YOLO coordinates are fractions, so the grid comes from each plane's own JPEG header, read at
  `read` and checked against the first image's shape; another shape raises, naming the file. A vertex
  `(x, y)` is at `(x * width, y * height)` pixels, with pixel `(row i, column j)` covering `[j, j + 1) x
  [i, i + 1)`; **a pixel belongs to a polygon when its centre is inside it** (even-odd rule; the last vertex
  joins the first). A polygon thinner than a pixel may cover no centre and paint nothing.
- **Ids** (`polygons` and `classes`: required for labels, refused for images):
  - `polygons: instance`: each polygon is filled with its 1-based row number in its file (blank lines are not
    counted); ids restart in every file (plane). `polygons: semantic`: each polygon is filled with its class
    id + 1, so class 0 is 1. Background is 0.
  - `classes`: the number of classes (`nc`). A row whose class is not a whole number in `0..classes-1` raises,
    so a class the record does not know is found rather than painted.
  - Polygons are painted largest first (by pixels covered), so where two overlap the smaller one is on top
    (file order breaks ties).
  - Rows that are not polygons raise: a row of a class and 4 numbers (a YOLO box, `cx cy w h`), an odd number
    of coordinates, fewer than 3 vertices, a coordinate outside `0..1` or not a number.
  - **Exif orientation.** The JPEG is returned as stored; an Exif `Orientation` is not applied. A label file
    *with* polygons whose JPEG has an orientation other than 1 raises: its stored pixels are turned against
    the displayed picture, and the file does not say which of the two the polygons were drawn on.
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member is read through `io.open`; a
  deflated one raises `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads the zip's directory
  and the first image's header (labels: also the first label file), decoding nothing. A label plane costs its
  `.txt` and its JPEG's header: the first 64 KiB of a stored JPEG, the whole member of a deflated one (as in
  the example, about 2.7 MB), which `io` keeps, so the image plane of the same crop costs no more. The last
  plane made is kept, so the three `c` planes of an image decode once.
- **429 Too Many Requests.** A host that limits requests per minute and address answers 429 to a job that
  itself made few, when other jobs share its address (Zenodo: 133 requests a minute; the first crop job on
  this snippet was refused on about its ninth request). `io` retries dropped connections and 5xx answers but
  raises a 429 at once, so the snippet waits it out: a listing or file read that fails with a 429 sleeps for
  the answer's `Retry-After` seconds (60 when it has none; never under 1 or over 120) and is asked again, up
  to 5 times; after that the 429 is the error. Nothing else is retried here, and the bytes still all come
  through `io`. A file costs two requests (its 30-byte zip header and its data) the first time and none
  after, so one z plane read through the image and both labels is four requests.
- **dtype.** `uint8` for images, `uint16` for labels. **Voxel size:** `None` (JPEG densities are print sizes).

## Known limits

- Zip members only. A label url without `::` raises; an HTTP directory of label files is not supported.
- JPEG only (8-bit, 1 or 3 components); no PNG or TIFF images, no CMYK, no 12-bit.
- Shapes, classes and coordinates beyond the first file are checked at `read`, not at `open`.
- One polygon per row: the rows of YOLO box tables are refused, and a YOLO row that joins several parts of
  one object into a single vertex list is filled as that one self-crossing polygon (even-odd).
- More than 65,535 rows in a file raise. Instance ids are per file, not unique across planes.
- JPEG is lossy: image values carry compression noise.

## Example

`examples/zenodo-19830051-pollenbb16-chilean-pollen.yaml`: one 43.9 GB zip at Zenodo with 16198 brightfield
RGB JPEGs (2064x3088, deflated members) of acetolysed pollen from 16 species under `pollenbb16/images/` and one
YOLO segmentation `.txt` each under `pollenbb16/labels/`. Bound as one `zcyx` raw `(16198, 3, 2064, 3088)` and
two `zyx` labels on it, `polygons: instance` and `polygons: semantic`. While testing, every label file was read
from the zip by range (6 MB): 16198 files, none empty, 36,383 polygons of 6 to 33 vertices, class ids 0-15,
all coordinates within 0..1; two planes were read through all three arrays (19 and 2 instances), and the
outlines of the 19 drawn on their image lie around the grains. `harness crop --n 8` made 8 of 8 crops with no
error (`harness verify` faulted them only for the licence, which the issue's copy of the record leaves out);
the 429 wait was tested through the harness `IO` against a local server refusing three
requests in ten. Images were not all decoded: a JPEG of another
shape would raise at `read`. The example binding's `snippets_commit` is the commit before this snippet's own
(a binding can't name the commit that adds it); a binding written by the worker names a commit that contains
the snippet.
