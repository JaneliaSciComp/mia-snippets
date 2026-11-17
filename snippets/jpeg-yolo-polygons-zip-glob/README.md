# jpeg-yolo-polygons-zip-glob

Reads a YOLO segmentation set (the layout Roboflow and Ultralytics export) from a zip: a folder of JPEG images
and, beside it, one `.txt` of polygons per image. One array is either

- **the images** (no `polygons` param): one image per z plane, decoded with `imagecodecs`; an RGB JPEG's
  samples are the c axis (`zcyx`, c = R, G, B), a grey JPEG has none (`zyx`); or
- **their labels** (`polygons: instance | semantic`): for each of the same images, the polygons of its label
  file filled on that image's grid (`zyx`, `uint16`, 0 = background).

No catalog snippet read YOLO `.txt` labels before this one (`jpeg-zip-glob` reads the images only, the box
rasterisers `tiff-jpeg-voc-zip-glob` and `jpeg-xml-boxes-numbers` parse XML), and none could bind a folder whose
images differ in shape under hashed file names; `shape` does that here.

- **URLs.** Each URL names one zip: a plain `<zip url>`, or `<outer zip url>::<inner zip member>` for a zip
  nested in another (an outer member glob is expanded with `io.expand`). Several URLs are concatenated in
  binding order. The pattern for the images is a param, not a `::` part of the URL: the zip is opened once
  through `io.open` and read in `io`'s 1 MiB cached blocks, which costs far fewer requests than one `io.fetch`
  per member (Zenodo answers 429 to a burst of requests).
- **`glob`** (required) picks the **images**, also for a label array: an `fnmatch` pattern over member names
  (`*` also crosses `/`), matches sorted by name. Anchor it at the folder (`set/train/images/*.jpg`). A raw and
  its label given the same `glob` (and `shape`) hold the same images in the same order by construction.
- **Label files.** The label file of `<dir>/images/<stem>.jpg` is `<dir>/labels/<stem>.txt`: the last `images`
  folder of the path becomes `labels` and the extension `.txt`, the rule Ultralytics uses. Each non-blank line
  is `<class id> x1 y1 x2 y2 ...`, three or more vertices, x and y as fractions of the image width and height.
  An empty file and a missing file are both an all-background plane (YOLO's convention for an image without
  objects). A glob whose images are not under an `images/` folder raises at open when `polygons` is set.
- **`polygons`.** `instance` fills each polygon with its 1-based line number in its file, so ids restart in
  every plane. `semantic` fills it with its class id + 1. A pixel belongs to a polygon when its centre lies
  inside the closed outline (even-odd rule), with vertex (x, y) at `(x * width, y * height)` and pixel
  `(row, col)` spanning `[col, col + 1) x [row, row + 1)`. Polygons are painted largest first, so where two
  overlap the smaller one is on top and the other's claim to those pixels is lost.
- **`classes`** (required with `polygons`) is the number of classes, `nc` in the set's `data.yaml`. A class id
  that is not an integer in `0..classes-1` raises, so a class the binding does not know is found, not painted.
  Lines that are not polygons raise too: four numbers after the class id (a YOLO box `cx cy w h`), an odd
  count, or a coordinate outside `[0, 1]`.
- **`shape: [Y, X]`** keeps only the images of that shape. An array has one shape, so a folder of mixed shapes
  is bound as one array per shape that is worth binding; images of other shapes are left out of that array.
  With `shape`, `open_array` reads every matched image's JPEG frame header (its first 64 KiB, nothing decoded),
  which over HTTP touches a 1 MiB block per image: cheap for a folder of small images, close to a full
  download for a folder of large ones. Without `shape` only the first image's header is read, every image
  must have its shape, and one that differs raises at `read` naming itself (for a label plane the image's
  header is read then, since the fractions are of that image's width and height).
- **Access.** `plane`: an image is decoded, or a label file rasterised, whole whatever the region; the last
  plane is kept, so the c planes of one image cost one decode. A zip that `io.open` cannot read by range (a
  deflated inner zip, a server ignoring Range) raises `NeedsFetch` and is staged whole with `io.fetch`.
- **dtype.** `uint8` images (a 12-bit JPEG raises), `uint16` labels. **Voxel size:** `None`; a JPEG's JFIF
  density is a print size and the label files carry none.

## Known limits

- **The label ids are polygon fills, not the annotator's own raster.** A training pipeline that rasterises
  with OpenCV (`fillPoly` on rounded vertices, edges included) can differ from this by a pixel along outlines.
  A polygon thinner than a pixel can enclose no pixel centre; its id is then absent from the plane.
- Instance ids are line numbers per file: the same object in two tiles has unrelated ids, and an object cut by
  a tile border is one polygon per tile.
- Images of a shape that `shape` does not name are not in the array. Binding every shape of a very mixed
  folder takes one raw and one label entry per shape.
- JPEG is lossy; Exif orientation is not read (the decoder returns the stored pixel grid, which is what
  Roboflow exports are annotated on; none of the example's 1,497 bound haustoria images has an Exif block,
  the GUS tiles were not all looked at).
- YOLO box labels (`cx cy w h`), oriented boxes and keypoints are not read. PNG or TIFF images are not read.
- Shape and label checks beyond open happen at `read`, not at `open`.

## Example

`examples/zenodo-15570004-bluvision-haustoria-gus-yolo.yaml`: one 1.31 GB zip at zenodo.org with two Roboflow
YOLOv8 segmentation sets of GUS-stained barley and wheat epidermis, each split into train/valid/test, one
class each (`gus`, `hau`). The GUS set is 1052 / 301 / 150 RGB tiles, 2048x2048 per the record and in the tiles read while testing (the
first of each split and four of test; reading all 1,503 was stopped by Zenodo's 429 rate limit); the haustoria set is 1378 /
394 / 196 small cell crops, of which 1059 / 293 / 145 are 150x350 and the rest come in about 400 other shapes
of one to three images each. Bound as six `zcyx` raws and six instance labels; the haustoria raws take
`shape: [150, 350]`. The example binding's `snippets_commit` is the commit before this snippet's own (a binding
can't name the commit that adds it); a binding written by the worker names a commit that contains the snippet.
