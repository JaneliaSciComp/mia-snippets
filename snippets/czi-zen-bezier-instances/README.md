# czi-zen-bezier-instances

A **label form only**. Reads the closed outlines a **Zeiss CZI** carries in its own XML metadata, the ZEN graphics
elements `ImageDocument/Metadata/Layers/Layer/Elements/Bezier` (e.g. cells an annotator outlined by hand on an
Axio Scan slide), and fills each as one instance id on the `yx` grid of **one scene** of that CZI's mosaic.

`urls` is exactly one CZI at a plain HTTP(S) URL (S3 included): the same url as the raw it pairs with. No globs.
The pixels of that scene are another snippet's: `wsi-czi-svs-rgb` with the same `scene`. Nothing here decodes a
tile, so the tiles' pixel type and compression do not matter to it.

## What is read

No CZI library is on the harness allowlist, so `snippet.py` parses the file itself (`struct`): the `ZISRAWFILE`
header, the `ZISRAWDIRECTORY` subblock directory (for the grid) and the `ZISRAWMETADATA` XML (for the outlines
and the pixel size). All of it is read at open, by byte range (1 to 3 MiB in the tested files); a read costs no
bytes. A server without Range support has the file staged whole.

## The array

- `axes` `yx`, `dtype` `uint16` (`uint32` for a file with more than 65535 outlines). A pixel holds the id of the
  outline that encloses the pixel's centre, 0 where none does.
- **Grid.** `shape` is the bounding box of the full-resolution tiles of the scene (subblocks whose stored size
  equals the extent they cover; pyramid copies are not counted), which is the canvas `wsi-czi-svs-rgb` gives
  the same scene. So the label pairs with that raw pixel for pixel.
- **`scene`** (param, optional). The S index of the subblock directory. A file with several scenes is an error
  without it, and the error lists them; a file with one scene needs no param.
- **Coordinate frame.** ZEN graphics are taken as pixels of the whole image: x to the right and y down from the
  corner of the bounding box of the full-resolution tiles of **all** scenes. Each point is moved by the corner of
  the scene's own bounding box in that frame (in the tested files `y - 2164` and `y - 5400` for scene 0, x
  unchanged). See "Checked on" for how this was verified.
- **Ids.** The 1-based position of the `Bezier` element among all `Bezier` elements of the file, every layer and
  every scene, in file order. So an id names the same element whichever scene is opened, and the ids of one scene
  need not start at 1 or be consecutive. The element's own `Id` attribute is not used.
- **Fill.** The element's `Geometry/Points` (`x,y x,y ...`) are joined in order as a closed polygon, the last
  point to the first, and filled by the even-odd rule. An integer coordinate is taken as a pixel corner: pixel
  (row, column) has its centre at `(column + 0.5, row + 0.5)`. Where outlines overlap, the later element is kept.
  An outline that crosses the scene's edge is cut there. One with fewer than 3 points fills nothing and its id
  stays unused.
- **What is left out.** `Line` (ZEN distance measurements) and `ScaleBar` elements enclose nothing and are
  skipped. **Any other element kind is an error at open** that names it (a rectangle, ellipse, polygon, open
  curve, text ...): an area drawn with another tool would otherwise be missing from the label without notice.
- **Refused at open**, with the reason: a file with no `Bezier` element; a scene none of the outlines touches
  (the error says how many lie on each scene, so an unannotated scene cannot be bound as an empty label);
  `Points` that are not pairs of numbers; a multi-part file or one without a directory or metadata segment.
- **Voxel size.** `Metadata/Scaling/Items/Distance[@Id=X|Y]/Value`, which CZI states in metres, times 1e9. `None`
  when the file has no such element.
- **Access.** Every read is computed from the outline list, whatever its size. The array reports `plane` while
  the scene holds at most 2^29 pixels (a 1 GiB plane) and `chunk` above that. Outlines of a few cells are a tiny
  part of a slide, and the harness looks for annotated pixels over a whole plane only for a `plane` label (for a
  `chunk` label it tests the box it drew): on a 16320 x 21765 scene that search took 1.5 GB of memory in
  `harness crop` and put every other crop on an outline; on a 54098 x 56315 scene it would take about 13 GB, so
  that label is `chunk` and its crops hold an outline only by chance.

## Known limits

- The label says what was outlined, not what was not: 0 is "not outlined", and in the tested files almost all
  tissue is 0 (6 and 18 outlined cells per slide). It is not a full segmentation of any region.
- `Bezier` is ZEN's name for the element; its points are used as polygon vertices, not as spline control points.
  In the tested files consecutive points are 0.5 to 9 pixels apart (median 1.4 and 2.9), and the filled area is
  within 0.4% of the area ZEN stored with each outline, so the curve ZEN draws through them stays close to the
  polygon. A `Bezier` with a few widely spaced points would be filled as a coarse polygon.
- The sub-pixel convention (integer coordinate = pixel corner) is an assumption: moving the label by half a
  pixel or one pixel does not change the agreement with ZEN's measurements.
- In both tested files the smallest X and Y of the full-resolution tiles is 0, so "from the corner of the
  bounding box" and "in the directory's own coordinates" are the same frame there. The snippet uses the first
  (ZEN's image frame); a file whose tiles do not start at 0 was run on synthetic input only.
- Only files with several scenes whose outlines all lie on scene 0 were tested on real data. One-scene files,
  files without an S dimension, the staged (no Range) path, cut outlines and the refusals were run on synthetic
  CZIs built in memory.
- Whether a `Bezier` can be an open curve is not known; the tested ones all carry an Area measurement. An open
  one would be closed and filled.
- Outlines are not tied to a scene by the file: an outline is drawn on every scene its bounding box touches.

## Checked on figshare-34065699 (examples/)

`files/69638460` (24H28_s1.2.czi: 3 scenes, 446 full-resolution tiles of 1600 x 1200, 6 `Bezier`, 4 `Line`, 1
`ScaleBar`) and `files/69638433` (2 scenes, 1860 tiles, 18 `Bezier`, 9 `Line`, 1 `ScaleBar`). All outlines lie
on scene 0 of their file; scene 0 is `16320 x 21765` and `54098 x 56315`, the shapes `wsi-czi-svs-rgb` gives.

ZEN stored two measurements with every outline (`Attributes/MeasurementText`: `Area 139.675 µm²`, `Intensity
Mean Value 69.282`), which the snippet does not read and which test it independently:

- **Frame.** The raw of scene 0 read through `wsi-czi-svs-rgb`, averaged over its three samples inside each
  outline as this snippet fills it, against ZEN's Intensity Mean Value: rms difference 1.65 over the 6 outlines
  of the first file and 0.32 over the 18 of the second (correlation 1.00 in both). With the label moved 60
  pixels up, down, left or right the rms difference is 34 to 52 and 16 to 20. So the outlines are on the pixels
  ZEN measured: no transpose, flip or offset, and the scene shift is right (it is 2164 and 5400 pixels).
- **Area.** Filled pixels times the pixel area is 0.997 to 1.002 of ZEN's Area for all 24 outlines.
- **By eye.** Overlaid on the raw, each outline follows the border of one muscle cell cut across, around its
  nucleus.
- **Fill.** `_fill` gives the same pixels as a per-pixel even-odd test on 300 random polygons and windows
  (vertices on pixel centres and corners included).
- **Harness.** `harness smoke` on the example binding is ok. `harness crop --n 4` on scene 0 of the first file
  alone placed its two aimed crops on the outlines (`placement: annotation`), 1.5 GB peak memory.
