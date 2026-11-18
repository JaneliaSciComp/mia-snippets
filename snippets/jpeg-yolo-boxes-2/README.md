# jpeg-yolo-boxes-2

Reads a set of photographs as one array, one image per z plane (`z` is kept for a single image too), or the
**YOLO box tables** that belong to them as a label array on the same grid. It is `jpeg-yolo-boxes` with three
additions, and reads what that snippet reads the same way:

1. **A file is what its first bytes say.** JPEG or PNG, whatever the name: a PNG saved as `.jpg` is read as a
   PNG, in the same array as the JPEGs around it.
2. **`overhang`.** A box may reach a few pixels past the image edge and is clipped to it; without the param a
   box that leaves the image still raises.
3. **Exif orientation 0 is no orientation.** 0 is not a value Exif defines and viewers show such a file as
   stored, so it is read like a file without the tag (the older snippet asked for `drawn_on`).

The array is either

- **images**: 8-bit JPEG (`imagecodecs`, libjpeg) or 8-bit PNG (grey or RGB, with or without alpha; no palette,
  no 16-bit). The samples are kept: an RGB file is three `c` planes (`zcyx`, c = R, G, B), a grey file none
  (`zyx`). A PNG's alpha channel is left out when it is 255 everywhere, and raises otherwise; or
- **box tables**: one text file per image, one row per box, `class x_center y_center width height`, the class
  an integer id from 0 and the four numbers fractions of the image width and height (the YOLO detection
  format). Each table is rasterised (`zyx`, `uint16`) on the grid of **the image with the same file name stem**
  (`labels/<split>/<stem>.txt` -> `images/<split>/<stem>.jpg`, any folder, any extension), whose header is read
  for it. An empty table is an all-background plane.

- **URLs.** A plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>` or a
  glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch`; `*` also crosses `/`), expanded with
  `io.expand`. A url whose file name ends in `.txt` is a box table; every other url is an image.
  - **Raw:** the image urls. z is binding order, and name order within a glob.
  - **Labels:** *the image urls of the raw, in the raw's order*, and the urls of the tables (anywhere in the
    list). z follows the images, never the tables: plane k is the table of image k, found by stem. Open fails
    unless every image has exactly one table and every table an image (a stray `classes.txt` matched by a wide
    glob raises), and when two images share a stem. Anchor globs at their folder (`…/images/*.jpg`,
    `…/labels/*.txt`).
- **One array, one shape.** A set whose images come in several stored shapes is bound as one array per shape,
  each with its own globs (the example binding: three, by file name prefix).
- **`count`**: the number of images the array must have; open fails on another number. Give raw and labels
  the same.
- **Boxes** (`boxes` and `classes`: required for a label array, refused for a raw):
  - `classes`: the class names in class id order, the first is id 0. The tables hold ids only; the list says
    what they mean and how many there are. A row whose id is past the list raises.
  - `boxes: instance`: each box is filled with its 1-based row number in its table; ids restart in every
    plane. `boxes: semantic`: each box is filled with class id + 1 (its position in `classes`, from 1).
  - Background is 0. A box covers every pixel it touches: its edges `(centre ± size/2) * image size` are moved
    outwards to pixel boundaries (an edge within 1e-6 px of a boundary is on it, since the fractions are
    rounded decimals).
  - Boxes are painted largest first (by the area inside the image), so where two overlap the smaller one is on
    top (file order breaks ties).
  - A row that is not five fields (a YOLO polygon row has more), a class that is not a whole number, or a
    number that is not finite raises, naming file and line.
- **`overhang`** (label arrays only, default 0): how many pixels a box may reach beyond the image edge, measured
  on the pixel-boundary edges above. Such a box is clipped to the image. A box further out raises, naming the
  box, its pixels and how far it is over; so does a box with no width or height, or with nothing left inside
  the image. Tables converted from pixel corners are often off by a pixel at the edge (a converter that
  takes 1 from each centre puts a box drawn at the left or top edge 1 px outside); that is what the param is
  for. **Set it to the largest overhang found in the files, not higher**: boxes far
  outside mean the fractions are of another grid (a turned picture, another image), and a large `overhang`
  would hide that.
- **Exif orientation, `drawn_on`.** Images are returned **as stored**; an Exif `Orientation` (JPEG APP1, or a
  PNG `eXIf` chunk) is never applied to pixels. Phone photographs are often stored turned (orientation 6: the
  stored 2736x3648 is shown 3648 wide, 2736 high), and a box table does not say whether its fractions are of the
  stored grid or of the picture a viewer shows. `drawn_on: displayed` says the latter (what an annotation tool
  shows, and what YOLO training code that applies the orientation expects): each box is turned back onto the
  stored pixels by its own image's orientation (all of 2..8; width and height swap for 5..8), so it lies on the
  raw as this snippet returns it, and `overhang` is counted there. `drawn_on: stored` uses the fractions on the
  stored grid as they are. Without the param a table with boxes whose image has an orientation of 2 or more
  raises. An image without the tag, or with 0 or 1, is the same either way; a value above 8 raises with
  `drawn_on: displayed`. **Look before choosing**: draw the boxes both ways on one image.
- **The labels are boxes, not masks**: where a box was drawn, not the cell's outline. Whether a binding may call
  that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or Range-capable URL is read
  through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch` (one request).
  `open_array` reads the listings, the first image (header only, nothing decoded) and, for labels, the first
  table. Reading a label plane gets its table and its image (for shape and orientation); `io` keeps the image,
  so the raw plane of the same crop costs no more. The last decoded plane is kept, so its three `c` planes
  decode once.
- **dtype.** `uint8` for images, `uint16` for boxes. **Voxel size:** `None` (JPEG and PNG densities are print
  sizes).

## Known limits

- All images of one array must have the first image's stored shape and sample count; another shape raises at
  `read`, naming the file (images and labels alike). Shapes, formats, orientations and tables beyond the first
  are checked at `read`, not at open.
- An image without a table raises at open; the YOLO habit of leaving out the table of an image without objects
  is not read as background (an empty table is).
- YOLO polygon (segmentation), oriented-box and pose rows are not read. More than 65,535 boxes in a table raise.
- JPEG: 8-bit, 1 or 3 components; no CMYK, no 12-bit. PNG: 8-bit grey or RGB; no palette, no 16-bit, no
  transparency (an alpha channel must be 255 everywhere). No TIFF.
- A clipped box is shown as its part inside the image; the table's full extents are not recoverable from a
  plane, also because overlapping boxes hide each other (smaller on top).
- Tested on real data on deflated zip members only, on orientations absent, 0 and 6, and on RGBA PNGs with an
  opaque alpha. Stored members, grey and RGB PNGs, a PNG `eXIf` orientation, the refusals and equality with
  `jpeg-yolo-boxes` on files that snippet reads (orientations 1, 3 and 6, both `drawn_on` values, both `boxes`
  modes) were exercised on synthetic files over a local range server.

## Example

`examples/figshare-22679839-smartmalarianet-thin-smear-detection.yaml`: the 150 step 1 photographs of
SmartMalariaNET (Giemsa-stained thin blood films through a microscope eyepiece, three phone series in one
941 MB zip, all members deflated) and their YOLO tables of one class, as three raws with one `boxes: instance`
label each:

| files | images | stored (rows x columns) | what needed this snippet | boxes |
|---|---|---|---|---|
| `20*_S*.jpg` | 68 | 2736 x 3648 | 4 files are RGBA PNGs (alpha 255) named `.jpg`; 1 box 1 px past the left edge | 21,823 |
| `IMG_20200310_*.jpg` | 27 | 2736 x 3648 | Exif orientation 0 in all; 3 boxes 1 px past the top edge | 4,404 |
| `IMG_20200413_*.jpg` | 55 | 3648 x 2736 | Exif orientation 6 in all (`drawn_on: displayed`); 175 boxes 1 px past the displayed picture's left or top edge | 9,262 |

While testing, every plane of all six arrays was read through the snippet and compared with a separate reading
of the same files (the image decoded with `imagecodecs.imread`; the boxes painted on the displayed picture and
that picture turned to the stored grid with `np.rot90`): all 150 image planes and all 150 label planes were
equal, every one of the 35,489 boxes has pixels of its own, and `overhang: 1` clips 179 of them. `drawn_on` was
decided by looking: on `test/IMG_20200413_212946` the boxes drawn as fractions of the displayed picture are
near-square frames around single red cells, drawn as fractions of the stored grid they are tall rectangles
that straddle cells and gaps; the snippet's own raw and label planes of `train/IMG_20200413_223311` show the
same fit. The boxes of `test/IMG_20200310_115816` (orientation 0) and of the PNG `test/20200103_S226` surround
the cells on the stored grid. The example binding's `snippets_commit` is the commit before this snippet's own
(a binding can't name the commit that adds it); a binding written by the worker names a commit that contains
the snippet.
