# jpeg-png-cvat-xml-polygons-i239

Reads a folder of annotated photographs through its **CVAT for images 1.1** annotation file, and gives either
the images the file names or the polygons it holds for them, filled on each image's grid. One `<image>` element
is one z plane, so the image array and the label array of one binding entry pair plane for plane.

It is for a collection the other snippets cannot bind:

- the annotations are one XML per folder, root `<annotations>`, with an `<image name=... width=... height=...>`
  element per picture and `<polygon label=... points="x,y;x,y;...">` children. `jpeg-xml-boxes-numbers` and
  `tiff-jpeg-voc-zip-glob` read one VOC-like `<annotation>` file per image with `<object>` boxes and refuse
  this root element;
- the folder has **no directory index** (`harness list` of a `raw.githubusercontent.com` folder is a 404), so
  no glob can name the image files. The XML is the index: it names every annotated image; and
- the images are of **several sizes**, so they are not one stack: `width` and `height` choose one size.

## URLs

Each URL is an annotation file, never an image: a plain URL, or a zip member `<zip url>::<member>`. Several
URLs are concatenated in binding order (not sorted), and a file named twice raises.

The file must be XML with the root `<annotations>` and at least one `<image>` child (a CVAT for video file has
`<track>` elements instead and raises). Each `<image>` needs `name`, `width` and `height`. The image file is
looked for **beside the XML**: its URL is the XML's URL with the last path segment replaced by `name`
(percent-encoded for a plain URL: `IMG 1.jpg` is requested as `IMG%201.jpg`; spelled as it is for a zip
member). A `name` may hold sub-folders (`images/a.jpg`); one that leaves the folder (`..`, a leading `/`, a
backslash) raises, and so does an image that two `<image>` elements name. XML with a `DOCTYPE` or an entity
declaration is refused.

The XML files are staged whole with `io.fetch` (26 KB to 143 KB each in the example) and cached by `io`, so the
other arrays of a binding cost no more network for them.

## Planes

The planes are the `<image>` elements in URL order, then in document order (CVAT writes them by `id`; they are
not sorted by name here). When the elements are of more than one `width` × `height`, open fails and names the
sizes and their counts; `width` and `height` then choose the elements of one size, and a binding has one raw
entry, and one label entry, per size. Give the label entry the same URLs, `width` and `height` as its raw:
both arrays are then made from the same elements in the same order.

## Params

- `polygons` (`instance` | `semantic`): what makes the array the **label**. Without it the array is the images.
  - `instance`: each polygon is filled with its 1-based number among the polygons of its `<image>`, in
    document order. Ids restart in every image.
  - `semantic`: each polygon is filled with the 1-based position of its `label` in `classes`.
- `classes` (list of strings; label form only, and required for it): every `label` string the polygons of the
  bound images use, in class id order. A polygon whose label is not listed raises at open, in both modes, so
  a label the binding's author did not see is never painted as something else. A label that is listed but
  unused is fine (the `<meta>` of a CVAT file declares the task's labels, used or not; it is not read).
- `width`, `height` (integers, set together): the `<image>` attributes of the planes to read, see above.
  Optional when every image the URLs name has one size.

## What it returns

**Images** (no `polygons`):

- 8-bit JPEG (grey or 3 components) or 8-bit PNG (grey or RGB, with or without alpha), told apart by their
  first bytes, not by their names. An RGB file is three `c` planes in file order (R, G, B): `zcyx`,
  `(images, 3, height, width)`; grey files give `zyx`. `uint8`, values as decoded by `imagecodecs`, unchanged.
- **Alpha is left out only when it says nothing:** a PNG whose alpha sample is 255 at every pixel gives its
  colour samples, so RGBA PNGs stack with RGB JPEGs of the same size; any other alpha raises at `read`.
- The file must be the size its `<image>` element says, and the files of one array must agree in colour (all
  RGB or all grey, as the first). One that differs raises at `read`, naming the URL, rather than being cut,
  padded or converted.
- A JPEG whose **Exif orientation** is not 1 (or absent) raises: its stored pixels are turned against the
  picture a viewer shows, and whether CVAT drew the polygons on the one or the other depends on its version.
- `open_array` reads the XML files and the first image's header (its first 64 KB; nothing is decoded). `read`
  fetches and decodes the files of the planes it is asked for, whole. `access` is `plane`. The last decoded
  file is kept, so the three `c` planes of one image decode it once.

**Labels** (`polygons` set):

- `zyx`, `(images, height, width)`, `uint16`, 0 background. The grid is the `width` and `height` of the
  `<image>` element. **No image is read**, at open or later: a label plane costs no network beyond the XML.
- `points` are pixel coordinates of the image with the origin at the top-left corner of the top-left pixel, so
  pixel (row i, column j) covers x in [j, j+1) and y in [i, i+1). A pixel is inside a polygon when its centre
  (j + 0.5, i + 0.5) is, by the even-odd rule; a centre exactly on a left or top edge counts as inside, one on
  a right or bottom edge as outside. A polygon is cut at the image's edges.
- Where polygons overlap, the one with the higher `z_order` is on top, as CVAT draws them; of equal `z_order`
  the smaller one, then the later one. A polygon wholly covered by others leaves no pixel.
- An `<image>` without polygons is an all-background plane. An `<image>` that holds any other annotation
  element (`<box>`, `<polyline>`, `<points>`, `<ellipse>`, `<mask>`, `<tag>`, ...) raises at open in the label
  form, so no annotation is left out silently; the image form does not mind.
- `access` is `plane`: a plane is painted whole and kept, and the harness finds the annotated pixels on it.

**Voxel size** is `None` in both forms: JPEG and PNG densities describe print size and the XML has no scale.

## Known limits

- **Only the XML files are in the binding's `urls`.** The harness derives what it knows about sources from
  `urls`: a crop's `crop.json` carries the validators of the XML files (which fix what each z index means), and
  `harness stage` prefetches and records only them. The images are read through `io` (counted, host-checked,
  their validators in `io.validators`), but an offline re-stage from the cache cannot reach them. This is the
  limit `tiff-csv-index-shape-i247` has for the files its table names.
- **z depends on the XML.** If an annotation file gains, loses or reorders `<image>` elements, the same z names
  another image. Pin the URLs to a commit when that matters.
- Images that no `<image>` element names are not read; there is no way to list them.
- The label trusts the `width` and `height` of the `<image>` element; they are checked against the file only
  when the image form reads that plane (the harness reads the raw patch of every crop it writes).
- Only `<polygon>`. No boxes, polylines, points, ellipses, cuboids, RLE masks, skeletons or tags, no `<track>`
  (CVAT for video), no polygon attributes, `occluded` and `group_id` are not read. Even-odd filling leaves a
  self-crossing polygon's doubly covered part empty.
- Ids are per image. A crop shows the ids of its own plane only, so this is no limit for the harness, but two
  planes' id 1 are different objects.
- Each image is read whole into memory (a 4032×3024 RGB JPEG is 1 to 3 MB on the wire and 36 MB decoded). No
  16-bit, palette or 1/2/4-bit PNGs, no CMYK or 12-bit JPEGs, no TIFF. A PNG's `eXIf` orientation is not read.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour image yields single-sample
  crops.
- A plain URL with a query or fragment (`?raw=true`) raises: it has no folder to find the images in.
- Tested on real data on plain URLs only (RGB JPEGs with Exif orientation 1 or none, RGBA PNGs with an opaque
  alpha, `z_order` 0 everywhere, polygons that overlap only by slivers, one label per file). Zip members (stored and
  deflated), grey images, `z_order` and overlaps, names with spaces and sub-folders and every refusal were run
  on synthetic files.

## Example

`examples/github-jimut123-rv-pbs-wbc-instance-segmentation.yaml`: RV-PBS, phone-camera photographs of
Leishman-stained peripheral blood smears in ten class folders of a GitHub repository, each with a CVAT
`annotations.xml` whose polygons outline the white blood cells of that folder's class. The ten files name 583
images with 752 polygons, in two sizes:

| width × height | images | files | array |
|---|---|---|---|
| 4032 × 3024 | 575 | 569 JPEG, 6 RGBA PNG | raw `zcyx` 575 × 3 × 3024 × 4032, label `zyx` 575 × 3024 × 4032 |
| 3302 × 2476 | 8 | 8 RGBA PNG | raw `zcyx` 8 × 3 × 2476 × 3302, label `zyx` 8 × 2476 × 3302 |

Not bound: the 140 photographs of `TEST_MIXED/` (no annotation file, so nothing names them) and one JPEG of
`LYMPHOCYTES/` that its XML does not name.

Checked on the real files, all 583 planes read through the snippet:

- every image decodes to the size its `<image>` element says; the 569 JPEGs have Exif orientation 1 (442) or
  none (127), and the 14 PNGs are RGBA with alpha 255 at every pixel;
- every label plane holds exactly the ids 1..n of its n polygons, 752 in all, and the polygons per class are
  the record's (blast cell 153, neutrophil 116, myelocyte 95, eosinophil 93, band cell 63, lymphocyte 62,
  promyelocyte 56, monocyte 54, metamyelocyte 32, basophil 28). `polygons: semantic` gives each polygon the
  class id of its folder;
- each filled polygon equals, pixel for pixel, an independent ray-casting test of the pixel centres against
  its `points`, and its pixel count is within 0.03% of the area of its outline. The only pixels a polygon does
  not keep are slivers where the outlines of touching cells cross: 19 polygons in 16 images give up 1 to 391
  pixels (under 0.1% of the polygon) to a smaller neighbour;
- the polygons lie on the stained cells: the green sample is darker inside every one of the 752 polygons than
  on the background within 150 pixels around it, by 52 to 207 grey levels (median 125);
- reading everything costs 629 MB.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
