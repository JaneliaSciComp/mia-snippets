# imod-model-mask

Rasterizes one object of an IMOD binary model (`.mod`, `IMODV1.2`: the contour models 3dmod saves) onto the
model's own image grid and returns it as a `zyx` uint8 mask of 0 and 1. A model holds polygons, not voxels, so
this is the one snippet whose `read` computes its planes; it does so the same way for every read, from the
contour points alone.

- **Grid.** `shape` is the image size in the model header (`xmax, ymax, zmax` → `(z, y, x)`): the size of the
  image the model was drawn on. Point coordinates are taken as pixel indices of that image, which is true when
  the model was drawn on the whole, unbinned stack. Smoke's label-vs-raw shape check is the guard: bind the
  model only to the raw it was drawn on.
- **Coordinates.** A point `(x, y, z)` lies on section `floor(z + 0.5)` (0-based, IMOD's own rounding). Pixel `i`
  covers `[i, i + 1)` in x. IMOD's y runs up from the bottom row of the image, array rows run down from the
  top, so a point's row coordinate is `ny - y`. Checked on EMPIAR-10553 by overlaying the outlines on the raw
  TIFF planes (see the example).
- **`draw: fill`** (default). A pixel is 1 when its centre is inside an odd number of the object's contours
  on that section (even-odd rule), so a contour nested inside another is a hole, as in IMOD's meshing. Contours
  with fewer than 3 points are ignored.
- **`draw: outline`.** A pixel is 1 when a contour line passes through it (each segment sampled every half
  pixel; closed contours include the closing segment). A one-point contour marks one pixel. Lines are one
  pixel wide; the snippet does not thicken them.
- **Several models.** With several URLs (e.g. one model per annotated cell of the same stack) each model is
  rasterized on its own and the mask is 1 where any of them is. All models must have the same header dims.
- **Access.** `file`: the 240-byte header is read at open; the first `read` reads the whole model (a few MB)
  and keeps the selected object's contours in memory. Later reads cost no bytes.
- **Voxel size.** Always `None`. A model's `pixsize`, `units` and `zscale` are typed in by the modeller for
  measurement and display and are not the image's calibration (the two EMPIAR-10553 models of one stack carry
  z scales 4.49 and 4.05).

## Params

- `object` (int): the 1-based object number, as 3dmod's object list and `imodinfo` show it. One binding entry
  per object, each a binary label. With several URLs the same number is used in every model.
- `objects` (str): for several URLs whose models number the same class differently: one number per URL, in
  the binding's URL order, e.g. `"2,4"`. Exactly one of `object` and `objects` is set.
- `draw` (str): `fill` or `outline`, above. Choose by what the class is: a contour drawn around a body
  (nucleoid, granule, whole cell) is filled; a contour that traces a thin structure (a membrane) is that
  structure only as an outline, and filled it is the region the structure encloses.

Object numbers and names are per model: read them from the model (`imodinfo`, or the `OBJT` chunks: 64-byte
name at the start of each) before binding, and say in `reasons` which name each number is. The opened array
carries the name it found as `object_name` after the first read.

## Known limits

- Only contours. Scattered-point objects are refused; meshes (`MESH` chunks) are skipped, so a mask is not
  interpolated between sections: a section with no contour is empty, even between two drawn sections.
- Contours must be planar in z (after rounding); a contour spanning sections is refused, as is a model with
  the flipped-y/z flag.
- `fill` refuses open objects and open contours (no inside); use `outline` for those.
- Contour flags other than "open" are ignored. In particular the snippet does not tell hand-drawn contours
  from ones IMOD's interpolator generated: in EMPIAR-10553's C1 model 84 of the 94 cell-envelope contours carry
  contour flag `0x20`, which looks like such a marker, so "manual" there may mean 10 drawn sections and the
  rest interpolated.
- Objects that overlap (a nucleoid inside a cell envelope) stay separate binary masks; the snippet builds no
  class-id or instance map. Contours of one object that are separate surfaces are not numbered as instances.
- The `MINX` chunk (model-to-image transform) is not applied. A model saved on a binned or sub-area load of
  the image has other header dims and fails the shape check rather than being rescaled.
- ASCII models (`imod` text format) and older binary versions are refused.

## Example

`examples/empiar-10553-gemmata-fib-sem-membranes.yaml`: the 470×1602×2268 FIB-SEM stack with its two
models (cells C1 and C2), four classes each. The example binding's `snippets_commit` is the commit before
this snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a
commit that contains the snippet.
