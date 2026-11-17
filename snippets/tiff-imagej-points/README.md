# tiff-imagej-points

Reads one single-plane **TIFF** image as `yx` (or `cyx`), and, as its label form, the points of **ImageJ point /
multi-point `.roi`** files marked on the pixel grid of such a TIFF.

No ImageJ ROI library is on the harness allowlist, so `snippet.py` parses the `.roi` itself (`struct`): the
64-byte big-endian header (`Iout`, version, type, bounding box, point count, options), the coordinate block
after it, and through the second header the per-point counter block.

## Raw form: `urls: [<one TIFF>]`, no params

- **What is read.** `series[0]` of the TIFF (tifffile), which must be one image plane: `Y`, `X` and at most one
  sample (`S`, e.g. RGB) or channel (`C`) axis, which becomes `c` in front (`cyx`). Size-1 axes are dropped. A
  z-stack or time series is refused (use `tiff-zip-member-2`).
- **URL.** A plain file or a zip member `<zip url>::<member>`. A stored member is read by range (`access:
  plane`); a deflated one raises `NeedsFetch` and is staged whole (`access: file`). The plane is decoded once,
  on the first `read`.
- **Voxel size.** OME `PhysicalSizeX/Y`, or ImageJ `unit` with `X/YResolution`; otherwise `None`.

## Label form: `urls: [<.roi> ..., <one TIFF>]`

The urls ending in `.roi` are the ROIs; exactly one other url is the TIFF they were drawn on. A `.roi` holds
coordinates only, so the TIFF's header gives the label its `shape` (the image's y and x, also for an RGB image)
and voxel size; no pixel of the TIFF is read. A glob url (`<zip url>::<folder>/*.roi`) is expanded with
`io.expand`. The `.roi` files (a few hundred bytes each) are read and checked at open.

- **Points.** Only ROI type 10 (point; a multi-point ROI is the same type with several points); any other type
  raises. Coordinates are image pixels, x right and y down from the image's corner. A ROI with sub-pixel
  resolution (option bit 128, version ≥ 222) gives floats, otherwise the integer block is used (bounding-box
  corner + offset). A point marks the pixel it lies in: `(row, col) = (floor(y), floor(x))`. A point outside
  the image raises: the ROI was drawn on another grid.
- **Result.** `yx` `uint16`, 0 where no point is. With several `.roi` urls the points are taken through the
  files in sorted url order, each file in its own point order.
- **`ids`.** `point` (default): each point carries its 1-based number in that order, so every point is its own
  instance and the points can be recovered from the label. `counter`: each point carries 1 + the index of the
  ImageJ multi-point **counter** it was clicked with (the tool's "Counter" choice, 0 to 255, low byte of the
  per-point counter block that version ≥ 228 files have), for ROIs whose counters stand for classes. A file
  without a counter block has counter 0 everywhere.
- **`radius`.** 0 (default): one pixel per point. `r` (1 to 64): the disc of pixels whose centres are within
  `r` pixels of the point's pixel, clipped at the image edge; where discs overlap the later point is kept.
  A disc is a marker of where the point is, chosen in the binding; it is not the extent of the annotated
  structure, which a point ROI does not hold.
- **`points`.** The opened array also carries `points`, a list of `{roi, x, y, row, col, counter, position,
  id}` per point (`position` is the 1-based stack slice in the counter block, 0 for none).
- **Access.** `plane`; the label plane is painted on the first `read` and costs no further bytes.

## Known limits

- The snippet cannot know which image of a folder a ROI was drawn on. It checks only that every point lies
  inside the bound TIFF; the binding's `reasons` say why that TIFF is the ROI's image.
- A point's `position` (stack slice) is not used: the grid is one plane. A ROI drawn on a stack is marked on
  the single plane it is bound to.
- Points only: area and line ROIs raise (`czi-mosaic-imagej-roi` fills area ROIs, on a CZI's grid). ROI
  properties, names, colours and point styles are not read. ImageJ `RoiSet.zip` archives are not unpacked:
  name the `.roi` members.
- A ROI embedded in a TIFF's ImageJ metadata is not read.
- The raw form holds the whole decoded plane in memory; it is meant for single images, not mosaics.

## Example

`examples/zenodo-13880404-lymphatic-capillary-junction-annotations.yaml`: one image folder of Zenodo 13880404
Dataset A (a 2.08 GB zip, members deflated): the C1 and C2 maximum projections (2048x2048 uint8, 60.23 nm)
and the folder's `1258-1491.roi`, a 19-point multi-point ROI marking cell-cell junctions. While testing, 53 of
the zip's 85 `.roi` files were parsed (Zenodo then answered 429): all are type 10, version 228, sub-pixel, 6 to
38 points each (1044 in all), and **every point has counter 0**, so the files do not hold the junction class
(`ids: counter` would give one class); 46 points in 3 files carry stack position 2, the rest 0. On an
overlay of the C1 image the 19 points all lie inside the vessel, on or next to its dark (stained) lines. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
