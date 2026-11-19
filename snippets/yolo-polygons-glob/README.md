# yolo-polygons-glob

Reads a set of YOLO segmentation label files as one label array, one file per z plane (`z` is dropped for a
single file), `uint16`, on the grid of the images the files annotate. No catalog snippet read YOLO text before
this one: the box rasterisers (`jpeg-xml-boxes-numbers`, `tiff-jpeg-voc-zip-glob`) read VOC XML only.

A YOLO segmentation file is text, one polygon per line:

```
<class id> <x1> <y1> <x2> <y2> ... <xn> <yn>
```

with every coordinate divided by the image's width (x) or height (y). The file says neither how large the
image is nor what the class ids mean, so both come from the binding.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand`, sorted by name; several URLs are concatenated in
  binding order.
- **`images`** (required): the URLs of the annotated images, written like the label URLs and expanded the same
  way; normally the `urls` of the raw the label pairs with. Label file k is rasterised on the grid of image k,
  read from the image's header: PNG `IHDR`, JPEG start-of-frame or the first page of a TIFF, told apart by the
  first bytes. No pixel is decoded. The two lists must be equally long, and position k of each must have the
  same file name without directories and extension (`labels_segment/<id>.txt` and `images/<id>.png`), which is
  how YOLO itself pairs them; anything else raises at `open_array`. Every image must have the first image's
  size, checked when its plane is read.
- **`fill`** (required):
  - `instance`: each polygon is filled with its 1-based number among the file's polygons (blank lines are not
    counted). Ids restart in every file (plane).
  - `class`: each polygon is filled with its YOLO class id + 1, because 0 is background.
- **`classes`** (optional): the class names in YOLO id order. They are not in the label files (YOLO keeps them
  in a `data.yaml`, which a dataset may not ship), so the binding states them and says where they come from.
  A polygon whose class id is not in the list raises, in both modes, so a class the record does not know is
  found rather than dropped. The names themselves are not used for the values.
- **Which pixels a polygon covers.** `x * width` and `y * height` are taken as pixel indices (column, row), the
  convention of the tools that write and read these files: a mask's contour as OpenCV traces it is the list of
  its boundary pixels, and Ultralytics rasterises a polygon by scaling it to the image and filling it with
  OpenCV. A pixel is labelled when its index point lies inside the polygon (even-odd rule) or on its outline.
  A contour traced from a mask therefore gives that mask back, boundary pixels included. Two tolerances make
  "on the outline" mean what the file meant: the text keeps 6 decimals, so a coordinate within `1e-6 x size`
  pixels of a whole pixel is taken as that pixel (0.003 px for a 2560-pixel side), and an edge's crossing with
  a row is compared with `1e-9` px of slack for float round-off.
- **Overlap.** Polygons are painted largest first (by polygon area), so where two overlap the smaller one is
  on top; file order breaks ties. An overlapped pixel carries one value.
- **Empty file.** A file without polygons (an image with nothing to annotate) is an all-background plane.
- **Refused.** A line with 4 values after the class (a YOLO box, `cx cy w h`), fewer than 3 points, an odd
  number of coordinates, a class id that is not a non-negative integer, a coordinate outside 0..1 (not
  normalised) or not a number; more than 65,535 polygons in a file; an image that is not PNG, JPEG or TIFF.
- **Access.** `plane`: one label file is one plane, painted whole on the first read of it and kept until
  another plane is read. A stored member or a Range-capable plain URL is read through `io.open`; a deflated
  member raises `NeedsFetch` and is staged whole with `io.fetch`. `open_array` lists the containers and reads
  the first image's header only.
- **dtype** `uint16`. **Voxel size:** `None`; YOLO text has none.

## Known limits

- A plane's grid costs its image's header. For a stored member or a plain URL that is a range read of a few
  bytes (a JPEG is read whole to find its frame header); for a **deflated** member it is the whole image,
  staged with `io.fetch`. The harness reads a label plane together with its raw plane, which stages the same
  file, so the cache serves it once; a job that reads labels only pays for the images.
- The pairing check compares file names, not content: it cannot tell that an image was replaced.
- Label files are checked when read, not at `open`: a bad line in file 200 raises when plane 200 is read.
- YOLO box files (`class cx cy w h`), oriented boxes, keypoints and `data.yaml` are not read. A 4-point
  oriented box (`class x1 y1 x2 y2 x3 y3 x4 y4`) has the shape of a polygon line and would be filled as one.
- A JPEG's EXIF orientation is not applied: the grid is the stored frame, as `image-tiles-glob` decodes it.
- Files written with fewer than 6 decimals do not snap to whole pixels; their polygons are filled as written,
  which can differ from the traced mask by boundary pixels.
- A hole can only be written as a second outline joined to the first by a bridge walked twice. Under the
  even-odd rule the hole stays empty, but the bridge is outline and is labelled, as a line one pixel wide.

## Example

`examples/zenodo-21691676-dart-fiducial-markers-microfluidic.yaml`: an 880 MB zip (members deflated) of 319
phase-contrast images (2160x2560 `uint8` PNG; 80 test, 239 train) of microfluidic chip fiducial markers, with
one YOLO segmentation file per image. The raw is read by `image-tiles-glob`; this snippet reads the label
files twice, `fill: instance` and `fill: class`. All 319 files were read while testing: 942 polygons of 100 to
236 vertices (5,395 to 18,814 pixels each), 2 to 4 per file, 33 files empty; 472 of class 0 and 470 of class
1, which the record's README counts as crosses and circles; every vertex a whole pixel; no two polygons
overlap; and each polygon's vertex extent equals its box in `labels_bbox/` (all 942). The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
