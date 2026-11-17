# imagej-roi-zip-tiff-grid

Reads one **ImageJ ROI zip** (a RoiSet: one `.roi` per object, as ImageJ's ROI Manager and Cellpose's
`--save_rois` write it) as a `yx` instance label on the pixel grid of the **TIFF** the ROIs were drawn on.

It is a label reader only; bind the TIFF itself as raw with a TIFF snippet (`tiff-zip-member-2`).
`czi-mosaic-imagej-roi` fills loose `.roi` files onto a CZI's grid; this snippet takes the ROIs as one zip and
the grid from a TIFF. No ImageJ ROI library is on the harness allowlist, so `snippet.py` parses the `.roi`
files itself (`struct`): the 64-byte big-endian header and the coordinate block after it.

## `urls: [<ROI zip>, <its TIFF>]`

Exactly two urls, in any order, no globs. The one whose file name ends in `.zip` is the ROI zip, the other is
the TIFF. Each is a plain URL or a zip member (`<zip url>::<member>`), so the ROI zip may be a zip nested in a
zip.

- **The TIFF** gives the grid: the Y and X size of tifffile's `series[0]` and the y/x voxel size (OME
  `PhysicalSize{X,Y}`, or ImageJ `unit` with `X/YResolution`; `None` otherwise). None of its pixels are read. A
  stored member or a plain file is opened by range (header only); a deflated member is staged whole, which
  costs nothing extra when the same member is the binding's raw (the harness stages a file once).
- **The ROI zip** is read at open (stored and read by range, or staged whole when it is a deflated member).
  Members not ending in `.roi` are ignored; a zip without any is an error.
- **The array** is `yx`, `uint16` (`uint32` for more than 65,535 ROIs), with the TIFF's y and x size and voxel
  size. A pixel holds the id of the ROI that covers it, 0 where none does. An ROI's **id is its 1-based
  position among the `.roi` members in the zip's directory**, the order the ROI Manager lists them in and
  ImageJ measures them in. Where two ROIs overlap, the later one is kept.
- **ROI types.** Polygon, freehand and traced outlines (types 0, 7, 8; subtype 0), with the float coordinates
  when the header says they follow (sub-pixel option, version ≥ 222), else the int16 ones added to the bounding
  box corner. Anything else (rectangle, oval, composite, line, point, ellipse, rotated rectangle, text, …) is
  refused at open with its type, as is an outline of fewer than 3 points.
- **Wrong image guard.** An ROI that lies wholly outside the TIFF's grid is an error at open. ROIs partly
  outside are clipped.
- **Access `plane`.** All outlines are filled on the first `read` and kept as one mask per ROI (0.1 s for the
  example's 94 outlines of about 900 points each).
- The ROI header's channel/slice/frame position is ignored: the label is one `yx` plane.

## `vertices`: what an outline coordinate is

An `.roi` stores vertices, not pixels, and two writers mean different things by an integer vertex.

- `corners` (default): **ImageJ's convention.** An integer coordinate is a pixel corner, pixel `(x, y)` spans
  `[x, x+1) x [y, y+1)`, and a pixel belongs to the ROI when the outline encloses its centre (even-odd rule).
  Right for ROIs drawn in ImageJ or made there from a mask (wand, Analyze Particles).
- `pixels`: **an integer coordinate is the pixel of that index.** A pixel belongs to the ROI when the outline
  passes through it (each edge is walked at unit steps along its longer axis and rounded) or encloses it
  (even-odd rule at the pixel indices). Right for Cellpose's ROI export, which writes the boundary pixels of
  each mask (`outlines_list`) as the vertices: filled this way an ROI is the outline pixels plus what they
  enclose, i.e. the Cellpose mask (minus any holes and any part not connected to its largest piece, which the
  outline never held). Filled as `corners`, such an ROI loses its bottom and right boundary pixels.

How to tell which a file needs: Cellpose-style ROIs have integer coordinates, every vertex one pixel step
(straight or diagonal) from the next, and a header `right`/`bottom` equal to the largest x/y rather than one
past it. Then check the fill: with the right setting touching objects abut without a gap or an overlap. Say in
`reasons` what was found.

## Known limits

- With `corners` the mask is close to, but not pixel for pixel, ImageJ's own: where an edge crosses a pixel
  centre exactly (every diagonal unit step does), ImageJ's filler and this one round differently. On the
  example's first three ROIs ImageJ counted 60,391, 50,926 and 49,392 pixels (RawIntDen / Mean in the record's
  result table) and `corners` fills 60,325, 50,929 and 49,296: within 0.2%, all on outline pixels.
- Only freehand ROIs with integer coordinates, filled as `pixels`, from a staged (deflated) zip member were
  read through the harness on real data. Float coordinates, polygon and traced types, clipping at the grid
  edge, the refusals and the `io.open` path (a ROI zip or TIFF readable by range) were run on synthetic input
  only; there both fills agree with a per-edge even-odd reference on 300 random polygons.
- One ROI zip per array, one TIFF series (`series[0]`), one plane: ROIs placed on different slices or channels
  of a stack all land on the same `yx` label.
- ROI names, colours and overlay properties are not read.

## Tested on

`zenodo-18886624-bio407-cos7-dfx-cellpose-rois` (examples/): `1_Control_P1_rois.zip`, a stored 311,514-byte
zip that is itself a deflated member of the record's `Upload to Zenodo.zip`, holding 94 `.roi` files (version
226, type 7 freehand, integer coordinates, 386 to 1,415 vertices each); and
`1_Control_P1.tif` (ImageJ hyperstack cyx 3x2048x2048 uint16, 162.38 nm per pixel, deflated in the zip).
Bound with `vertices: pixels`.

- **Convention.** In all 94 ROIs every vertex is exactly one pixel step from the next, and `right`/`bottom` are
  the largest x/y. As `pixels` the cells cover 3,815,395 px with no pixel in two cells and 103 one-pixel gaps
  between cells in the whole field; as `corners` they cover 3,780,759 px and 31,576 such gaps open, one along
  every boundary two cells share.
- **Frame.** Mean raw intensity on the label's cell boundaries, relative to the image mean: DAPI 0.41,
  TOMM20 0.77, SiR-actin 1.28 as read; 0.48 to 0.55, 0.88 to 0.91 and 1.16 to 1.20 with the label moved 20 px
  in any direction; 0.97 to 1.10 for all three with it transposed or flipped. So the outlines run between
  cells (dark in the nuclear and mitochondrial channels, bright in cortical actin) where the files put them,
  with no flip, transpose or offset.
- **Ids.** The record's result table lists the 94 ROIs of this image in the zip's directory order, so id `k`
  is row `k` of ImageJ's measurements for it.
- `harness smoke`: raw `[3, 2048, 2048]`, label `[2048, 2048]`, 19.4 MB fetched. `harness describe` on the
  central 1024x1024: 91% labelled (the cells are confluent), 36 ids, 31 of them one 4-connected component
  and at most 4 components per id. Why 5 ids have more was not looked into; my reading is cells cut by the
  window's edge or joined only diagonally, which an outline of 8-connected boundary pixels allows.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
