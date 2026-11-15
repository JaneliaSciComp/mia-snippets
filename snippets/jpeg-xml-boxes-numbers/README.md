# jpeg-xml-boxes-numbers

Reads a set of numbered 2D files as one array, one file per z plane (`z` is kept for a single file too). The
files of one array are either

- **images**: 8-bit JPEG (`imagecodecs`, libjpeg). The samples are kept: an RGB file is three `c` planes
  (`zcyx`, c = R, G, B), a grey file none (`zyx`); or
- **XML box annotations** of the VOC-like kind that has **no `<size>`**: a root `<annotation>` with `<object>`s
  holding a class string in `<label>` (or, without one, `<name>`) and a `<bndbox>` of
  `xmin`/`ymin`/`xmax`/`ymax`. Each file is rasterised (`zyx`, `uint16`) on the grid of **the JPEG of the same
  name** (`<stem>.xml` -> `<stem>.jpg`, extension from `image_ext`), whose header is read for it. A file
  without `<object>` is an all-background plane.

Which of the two is decided by the first file's leading bytes (`<` or not), never by its name.

It differs from `tiff-jpeg-voc-zip-glob` in three ways: the grid comes from the paired JPEG instead of
`<size>`, the class from `<label>`, and **the files of one array can be chosen by file number**, which is what
a set with several image shapes under one numbered name pattern needs.

- **URLs.** A plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>` or a
  glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch`; `*` also crosses `/`). Globs are
  expanded with `io.expand` and sorted **by stem**, so `…-*.jpg` and `…-*.xml` over the same stems pair plane
  for plane. The snippet cannot check that across arrays; the binding's `reasons` say why they pair.
- **One array per shape: `numbers`, `skip`, `count`.** An array has one shape, and every file must have the
  first file's. A file's number is the last run of digits in its name. `numbers: [1, 13, 60-61]` keeps those
  files; `skip: [...]` keeps all the others; both take numbers and inclusive `a-b` runs, and every number
  listed must be a file's (a file that went missing raises instead of shifting z). `count` is the number of
  files the array must end up with. Give the image array and its annotation array the same `numbers`/`skip`.
  **Why the binding has to say it:** shapes are in the JPEG headers only, and reading 1217 headers of deflated
  members at every open means fetching the whole zip. The list is checked where it is cheap: `open` reads the
  first file's header, and `read` checks every file it touches (an image's header before it is decoded; an
  annotation's JPEG header) and raises, naming the file, on another shape. A wrong list cannot produce a
  mis-shaped or shifted plane, only an error.
- **Boxes** (`boxes` and `classes`: required for XML, refused for images):
  - `classes`: every class string the files use, in class id order. An object with a string that is not listed
    raises, so a class the record does not know is found rather than dropped.
  - `boxes: instance`: each box is filled with its 1-based `<object>` number in its file; ids restart in every
    file (plane). `boxes: semantic`: each box is filled with the 1-based position of its class in `classes`.
  - Background is 0. A box covers the pixels `[ymin, ymax) x [xmin, xmax)`: corners are taken as 0-based with
    an exclusive far corner. The files do not say which convention their tool used; read as 1-based inclusive
    the same numbers would cover one more row and column at the near corner (boxes here are 134-329 px wide).
  - A fractional corner is moved outwards (`xmin`/`ymin` floored, `xmax`/`ymax` ceiled): every pixel the box
    touches. A box that is empty or reaches outside the JPEG raises.
  - Boxes are painted largest first, so where two overlap the smaller one is on top (file order breaks ties).
  - **Exif orientation.** The JPEG is returned as stored; an Exif `Orientation` is not applied. An annotation
    file *with* boxes whose JPEG has an orientation other than 1 raises: its stored pixels are turned against
    the displayed picture, and the file does not say which of the two the boxes were drawn on. A file without
    boxes is not affected.
  - An XML with a `DOCTYPE` or entity declaration, or a root other than `<annotation>`, raises.
- **The labels are boxes, not masks**: where a box was drawn, not the egg's outline. Whether a binding may call
  that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or Range-capable URL is read
  through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch`. `open_array`
  reads the first file (annotations: the first XML and its JPEG), decoding nothing. Reading an annotation
  plane also gets its JPEG (for the header); `io` keeps it, so the raw plane of the same crop costs no more.
  The last decoded file is kept, so its three `c` planes decode once.
- **dtype.** `uint8` for images, `uint16` for boxes. **Voxel size:** `None` (JPEG densities are print sizes).

## Known limits

- The shape groups are not discovered: someone has to read every JPEG header once and write `numbers`.
- Shape and class checks beyond the first file happen at `read`, not at `open`.
- JPEG only (8-bit, 1 or 3 components); no TIFF or PNG, no CMYK, no 12-bit.
- `<pose>`, `<truncated>`, `<occluded>` and `<source>` are not read. More than 65,535 objects in a file raise.
- Tested on zip members only (all deflated); plain URLs and directory globs go through the same `io` calls
  but were not exercised.

## Example

`examples/air-ug-intestinal-parasites-phonecamera-boxes.yaml`: a 459 MB zip (2434 deflated members) of 1217
smartphone-microscope JPEGs of stool wet preparations and one XML each. The JPEGs come in two shapes mixed
through the numbering (260 runs): 150 are 1224x1632x3 and 1067 are 1161x2064x3, so they are two `zcyx` raws,
`numbers` and `skip` of one list. All 162 boxes (Hookworm 129, Taenia 25, Hymenolepsis Nana 8; whole-number
corners) are in 147 XMLs of the 1224x1632 group; the other 3 XMLs of that group and all 1067 of the other are
empty. Each raw has its XMLs twice, `boxes: instance` and `boxes: semantic`. While testing, every plane of all
six arrays was read from a local copy of the zip (no file of another shape in either group, 147 planes with
boxes, 162 instances), and the box outlines were drawn on three images and lie around the eggs. The example
binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds
it); a binding written by the worker names a commit that contains the snippet.
