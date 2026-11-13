# tiff-jpeg-voc-zip-glob

Reads a set of 2D files as one array, one file per z plane (`z` is dropped for a single file). The files of
one array are either

- **images**: single-page TIFF (`tifffile`) or 8-bit JPEG (`imagecodecs`), mixed freely. The samples are kept:
  an RGB file is three `c` planes (`zcyx`, c = R, G, B), a grey file none (`zyx`). This is what it adds over
  `tiff-zip-glob-rgb`, which collapses the samples to one plane and reads no JPEG; or
- **Pascal VOC XML box annotations** (as written by labelImg), rasterised on the image grid that each file's
  `<size>` declares (`zyx`, `uint16`). No catalog snippet read boxes before this one.

Which of the two is decided by the first file's leading bytes (TIFF magic, JPEG SOI, `<`), never by its name.
An array that mixes images and annotations raises.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and its files are sorted **by stem** (the url without
  its extension, then the extension); several URLs are concatenated in binding order. Sorting by stem is what
  lets an image glob over mixed extensions (`Train set/*.[tj]*` for `.tif` and `.jpeg`) pair plane for plane
  with the annotation glob beside it (`Train set/*.xml`) when the two hold the same stems. The snippet cannot
  check that across arrays, so the binding's `reasons` must say why the stems pair.
- **Images.** Every file must be a single-series, single-page `YX` or `YXS` TIFF (not palette) or an 8-bit
  JPEG, and all files of one array must have the first file's shape and dtype; a file that differs raises at
  `read` and names itself. Files of another shape are bound as another array (another `raw` entry): an array
  has one shape, so "per-file shapes" means one array per shape. JPEGs are decoded by libjpeg to RGB.
- **Boxes** (`params`, both required for XML files and refused for images):
  - `classes`: every `<name>` the files use, in class id order. An object with a name that is not listed
    raises, so a class the record does not know is found rather than dropped.
  - `boxes: instance`: each box is filled with its 1-based `<object>` number in its file. Ids restart in
    every file (plane).
  - `boxes: semantic`: each box is filled with the 1-based position of its `<name>` in `classes`.
  - Background is 0. A box covers the pixels `[ymin, ymax) x [xmin, xmax)`: labelImg stores the corners of the
    drawn rectangle in 0-based image coordinates (it clamps `xmin`/`ymin` to 1 and lets `xmax`/`ymax` reach
    the width/height), so the far corner is exclusive. Read as the 1-based inclusive boxes of the original VOC
    devkit, the same numbers would cover one more column on the left and one more row on top.
  - Boxes are painted largest first, so where two overlap the smaller one is on top (file order breaks ties).
    An overlapped pixel carries one value; the other box's claim to it is lost.
  - A box that is empty, has a non-integer corner or lies outside `<size>` raises, as does an XML with a
    `DOCTYPE` or entity declaration or a root other than `<annotation>`.
- **The labels are boxes, not masks.** The array says where a box was drawn, not where the cell's outline is.
  Whether a binding may call this `instance` or `semantic` is a human's decision (the record's encoding is
  `boxes-table`, for which the binding has no `segmentation_type`); see the example binding's `reasons`.
- **Access.** `plane`: one file is one plane (all of its `c`). A stored member or a Range-capable plain URL is
  read through `io.open`; a deflated member raises `NeedsFetch` and is staged whole with `io.fetch`.
  `open_array` reads the first file only (a TIFF's header; a JPEG's or XML's bytes, without decoding pixels).
  The last decoded file is kept, so reading its three `c` planes in turn decodes it once.
- **dtype.** The image's own (`uint8` for JPEG); `uint16` for boxes. **Voxel size:** `None`; TIFF resolution
  tags and JPEG densities describe print size, and VOC XML has none.

## Known limits

- Over HTTP each deflated member costs two requests (local header, compressed bytes), made lazily per plane.
- Shape and class checks beyond the first file happen at `read`, not at `open`: opening every member of a
  deflated set would fetch all of it.
- No multi-page TIFF, planar (`SYX`) or palette TIFF, 12-bit JPEG, PNG, pyramid levels or voxel size.
- VOC fields other than `<size>`, `<object><name>` and `<bndbox>` (`difficult`, `truncated`, `pose`,
  `<filename>`) are not read; `<filename>` is not checked against the paired image.
- More than 65,535 objects in one file raise.

## Example

`examples/4tu-21695819-trypan-blue-insect-hek-cells.yaml`: a 1.4 GB zip (all members deflated) of trypan-blue
stained insect and HEK cell cultures. The 46 train and 14 validation images (42 RGB TIFF + 18 JPEG, all
768x1024x3 `uint8`) are bound as two `zcyx` raws, and their labelImg XMLs (9,041 boxes named Alive, Dead or
Aggregate) twice each: `boxes: instance` and `boxes: semantic`. The record lists only Alive and Dead;
Aggregate was found by this snippet's `classes` check. The unannotated test folders are not bound; their TIFFs (larger, 1544x2080 per the record)
would be further raws, one per shape. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
