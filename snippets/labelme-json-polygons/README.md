# labelme-json-polygons

Reads [LabelMe](https://github.com/wkentaro/labelme) JSON annotation files, one `<stem>.json` per image as the
labelme tool saves them, as label planes: each file's `shapes[]` polygons are filled on the grid the file
declares (`imageHeight` x `imageWidth`), `uint16`, one file per z plane (`zyx`; `yx` for a single file). No
catalog snippet read LabelMe JSON or filled polygons from JSON before this one. It reads annotations only: the
images beside them are bound through an image snippet (`image-tiles-glob` in the example).

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and its files are sorted **by stem** (the url without
  its extension, then the extension), as `tiff-jpeg-voc-zip-glob` sorts its, so `annotations/*.json` pairs
  plane for plane with an image glob over the same stems when the image snippet orders them alike. The snippet
  cannot check that across arrays, so the binding's `reasons` must say why the planes pair. Several URLs are concatenated in
  binding order.
- **Grid.** `imageHeight` x `imageWidth` of the file; they must be positive integers. All files of one array
  must declare the first file's size; a file that differs raises at `read` and names itself. Files of another
  size are bound as another array: an array has one shape, so a dataset whose images differ in size (the
  example's) is one label array per file.
- **Shapes.** Only `shape_type: polygon` (a shape without `shape_type`, as old labelme versions wrote, is a
  polygon). Any other type (`rectangle`, `circle`, `line`, `linestrip`, `point`, `points`, `mask`) raises
  rather than being dropped, as does a polygon with fewer than 3 points or a non-finite coordinate.
- **Pixels.** `points` are x, y in image pixel coordinates with pixel (row r, column c) covering
  `[c, c+1) x [r, r+1)`. A pixel belongs to a polygon when its centre `(c + 0.5, r + 0.5)` is inside the
  closed outline by the even-odd rule (a centre exactly on an edge belongs to the polygon on its right or
  below). This is the convention of `imod-model-mask` and `czi-mosaic-imagej-roi`. labelme's own
  `shape_to_mask` draws with PIL, which also paints the outline pixels: its masks can differ from these by
  about a pixel at the boundary. Parts of a polygon outside the grid are clipped.
- **Ids** (`params`):
  - `ids: instance` (the default): each polygon is filled with its 1-based number in its file's `shapes`
    list. Ids restart in every file (plane) and follow file order, which need not be the order of the labels.
    A shape with a `group_id` raises: labelme uses it to join several shapes into one instance, which this
    mode does not do.
  - `ids: semantic`: each polygon is filled with the 1-based position of its `label` in `classes`, which is
    then required.
  - `classes`: the labels the files use, in class id order. When given (either mode), a shape whose label is
    not listed raises, so a class the record does not know is found rather than dropped.
  - Background is 0.
- **Overlap.** Polygons are painted largest first (by polygon area; file order breaks ties), so where two
  overlap the smaller one is on top. This is what nested outlines need: in the example each annual ring is
  drawn as the outline of the whole stem up to that ring boundary, enclosing the earlier rings and the pith,
  and painting largest first leaves each year its ring and the pith its disc. An overlapped pixel carries one
  value; the larger polygon's claim to it is lost. Where hand-drawn outlines that should nest cross each
  other, the sliver of the smaller polygon that sticks out of the larger one keeps the smaller one's id, so
  an id can have more than one connected part.
- **Access.** `plane`: a LabelMe file has no header to read by range (`imageHeight`/`imageWidth` follow the
  embedded `imageData`), so any read costs its whole file. A stored member or Range-capable URL is read
  through `io.open`, a deflated member (`NeedsFetch`) is staged with `io.fetch`. `open_array` fetches and
  parses the first file and paints nothing; `read` paints only the y/x region asked for (rows of the region x
  polygon edges, never the whole plane): a 256-row strip across a 18434x17394 section took 0.1 s. The last
  parsed file's polygons are kept; the embedded image is dropped after parsing.
- **dtype.** `uint16`. **Voxel size:** `None`; a LabelMe file has no pixel size.

## Known limits

- The whole JSON is loaded and parsed for its polygons, including the base64 `imageData` when the file embeds
  the image (4 to 112 MB per file in the example dataset, several times that in memory while parsing).
- `imageData` is never decoded and `imagePath` is not checked against the paired image; the binding's
  `reasons` say why a file pairs with its raw.
- Size and class checks beyond the first file happen at `read`, not at `open`: opening every file of a set
  would fetch all of it.
- Self-intersecting outlines follow the even-odd rule: a loop the outline makes over itself is a hole.
- Shape `flags`, `description`, file-level `flags` and the labelme `version` are not read. Only version 5.5.0
  files were read on real data; `ids: semantic`, z stacks and globs were tested on synthetic files only.
- More than 65,535 shapes in one file raise.

## Example

`examples/zenodo-15311335-salix-glauca-annual-ring-sections.yaml`: the record's sample pair of 50 Salix glauca
stem cross-sections (Zenodo 15311335, one zip, every member deflated). Raw is `image-tiles-glob` on
`images/W_F07_C_S5.jpg` (`cyx`, 3 x 4430 x 4480 `uint8`); the label is this snippet on
`annotations/W_F07_C_S5.json`, 6 polygons (pith, 2016 to 2020) as instance ids 1 to 6 on the same 4430 x 4480
grid. The sections differ in size, so every pair is its own raw and label; the binding's `reasons` say why
only one is bound.
