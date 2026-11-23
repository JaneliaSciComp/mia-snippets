# tiff-json-class-points-glob-i311

Reads a set of **single-page TIFF images**, one file per z plane, and, as its label form, the **JSON point
annotations written for them one file per image**: an object whose keys are class names, each a list of
`{"x": ..., "y": ...}` object centroids as fractions of the image's width and height. Written for BreCaHAD
(`images/Case_<n>-<nn>.tif` with `groundTruth/Case_<n>-<nn>.json`):

```json
{"mitosis": [{"x": 0.4294117647058823, "y": 0.4609375}], "non_mitosis": [], "apoptosis": [...], "tumor": [...],
 "non_tumor": [...], "lumen": [...], "non_lumen": [...]}
```

The label is a points table, not a mask: it says where each object's centroid is and what class it has, not
which pixels the object covers.

Urls are zip members (`<zip url>::<member>`), plain URLs, or globs (`<zip url>::<glob>` over a zip's members,
`<dir url>/::<glob>` over an HTTP directory index; `fnmatch` syntax, `*` also crosses `/`). Each glob stands for
its matches sorted by name; the urls are kept in binding order.

## Image form: `urls: [<TIFFs or globs>]`, no params

- **Array.** `zcyx` for colour files (the samples of an RGB TIFF are `c` 0..2 = R, G, B), `zyx` for grey ones;
  z is the file, also when there is only one. Values and dtype are the file's own (uint8, uint16, ...), nothing
  is rescaled.
- **One shape and dtype per array.** `open_array` reads the first file's header only. A file whose shape or
  dtype differs from the first raises when it is read, naming the file. A set that mixes dtypes (BreCaHAD:
  uint16 for Cases 1-6, uint8 for Cases 7-17) is bound as one array per dtype.
- **What is refused.** A TIFF with more than one page or series, or whose axes are not `YX`, `YXS` or `SYX`
  (planar samples): see `tiff-zip-member` and its successors for stacks.
- **Access** is `plane`: a region costs its whole file. A stored member or a plain file is read by range; a
  deflated member is staged whole with `io.fetch`, once per dataset cache.
- **Voxel size** is `None`: a plain TIFF's resolution tags are a print size (BreCaHAD's files have none).

## Label form: `urls: [<JSON files or globs>]`, `params.classes` and `params.images`

- **Planes.** The images `params.images` names, in that order, each with the annotation file of its stem (the
  name without folders and extension: `images/Case_10-01.tif` and `groundTruth/Case_10-01.json`). Give
  `images` the urls of the image array exactly as its binding lists them, and plane k of the label is the
  annotation of plane k of that array. An image without an annotation file, an annotation file without an
  image, or two files of one stem raise at open.
- **Array.** `zyx`, each plane on the grid of its own TIFF, whose height and width are read from that TIFF's
  header (no pixel is decoded). All images must have the first one's height and width.
- **Placement.** A point at fractions `(x, y)` marks pixel `(row, column) = (floor(y * height), floor(x *
  width))`: x runs right and y down from the top-left corner, and pixel `(i, j)` covers x in `[j, j + 1)` and y
  in `[i, i + 1)`. A product within 1e-6 below a whole number is that whole number (`584 / 1360 * 1360` is
  `583.9999999999999` in floats), and a point on the right or bottom edge (1.0) is in the last column or row.
  A coordinate outside 0..1, or one that is not a number, raises: it means another frame.
- **Values.** The pixel holding a point is the point's class id, the 1-based position of its key in
  `params.classes` (`uint8`, `uint16` from 256 classes; bind as `semantic`). 0 is "no point here", not
  "background". Every key of a file must be listed in `classes`, so no annotation is dropped unseen and a
  misspelt class is noticed; a listed key a file does not have marks nothing there. Two points in one pixel:
  the one of the later class is kept, within a class the later in the file.
- **`radius`** (default 0) also marks every pixel whose centre is within that many pixels of a point's pixel (a
  disc), for a consumer that needs more than one pixel per object. A pixel in reach of several points takes
  the nearest one's class. The files state no size, so a disc is a drawing aid, not the object's extent.

## Params (label form only)

`classes` (what makes the array a label), `images`, `radius`: see `snippet.yaml`. The image form takes none.

## Known limits

- The label form reads every image's header to size its plane. For a deflated zip member that stages the whole
  member; in a binding that also binds the images as raw it is a cache hit.
- Shape and dtype are checked against the first file when a plane is read, not at open, so a set of mixed
  shapes opens and fails at the first file that differs.
- Class names are the JSON keys as the files spell them. BreCaHAD's files say `lumen` and `non_lumen` where its
  README and the record say `tubule` and `non_tubule`.
- Fields of a point other than `x` and `y` are ignored. A file is parsed whole in memory.
- Pixel-unit coordinates are not read; neither are polygons, boxes or any other JSON layout.
- Not run on real data, only on synthetic files over a local range server: stored zip members and plain URLs
  (read by range), grey and planar-sample TIFFs, `radius`, more than 255 classes, an HTTP directory glob.

## Tested on

`figshare-7379186-brecahad-breast-histopathology` (examples/): `BreCaHAD.zip`
(`https://ndownloader.figshare.com/files/14062469`), 488 members, all deflated, so every file is staged whole.

- **Images.** Every one of the 162 TIFFs was read through the snippet: all are single-page RGB, 1024x1360,
  LZW. The 51 files of Cases 1-6 are uint16 (`zcyx` 51x3x1024x1360, values 3..65535) and the 111 of Cases 7-17
  uint8 (`zcyx` 111x3x1024x1360, values 0..255), which confirms on every file the split the record inferred
  from file sizes; hence two raw arrays.
- **Annotations.** All 162 JSON files have the same seven keys in the same order: `mitosis`, `non_mitosis`,
  `apoptosis`, `tumor`, `non_tumor`, `lumen`, `non_lumen`. They hold 23,549 points, and the label planes hold
  23,549 marked pixels (7,608 on Cases 1-6, 15,941 on Cases 7-17): every point is inside its image and in a
  pixel of its own. Per class id: 1 mitosis 115, 2 non_mitosis 0 (the key is empty in every file), 3 apoptosis
  271, 4 tumor 20,155, 5 non_tumor 1,905, 6 lumen 493, 7 non_lumen 610. No plane is without points (5 to 335 per
  image). Pixel positions range over columns 1..1353 and rows 1..1015; 45% of the coordinates are not whole
  pixels, so positions are floored, not rounded.
- **Frame.** Mean of the three samples at the pixels of the nucleus classes (1, 3, 4, 5) against the same
  points moved: uint16 files 32,680 at the points, 35,547 to 39,077 moved 15 px up, down, left or right, 45,523
  to 45,896 with x, y or both mirrored, 45,592 with x and y exchanged, 46,121 over the whole images; uint8 files
  137.7 at the points, 153.0 to 165.9 moved 15 px, 185.7 to 187.5 mirrored or exchanged, 190.7 overall. The
  points sit on the dark (haematoxylin) nuclei with no flip, transpose or offset. Drawn on
  `images/Case_10-01.tif`, the 198 points fall on the dots of the dataset's own rendering
  `groundTruth_display/Case_10-01.png`, class for class.
- **Harness.** `smoke` ok (10,596,546 bytes from an empty cache; warnings only: raw 0 is uint16 where the
  record says uint8, and the record gives the label no dtype). Reading all 324 files cost 654,528,054 bytes.
  `crop --n 4` made 4 crops with their points in `labels/manual_gt-nucleus-points.png`, and `preview` drew
  them.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
