# imod-model-mask-2

Rasterizes one object of an IMOD binary model (`.mod`, `IMODV1.2`: the contour models 3dmod saves) onto the
model's own image grid and returns it as a `zyx` uint8 mask of 0 and 1. A model holds polygons, not voxels, so
`read` computes its planes; it does so the same way for every read, from the contour points alone.

Supersedes `imod-model-mask`. What changed, and why (EMPIAR-10554):

- **`open_contours`** (new param). `imod-model-mask` refuses to fill an object as soon as one of its contours
  carries the "open" flag. In 10554's C3 model one of the 39 poly-phosphate granule contours does (section 54:
  a complete 16-point circle whose ends are one polygon side, 7.6 px, apart, between closed contours of the
  same granule on sections 53 and 55), so the granule class could not be read from that model at all.
  `open_contours: close` fills such a contour as if it were closed, `skip` leaves it out, and the default
  `refuse` keeps the old error.
- **One-point contours draw nothing in an outline.** The old snippet marked one pixel for each. In a contour
  object a single point is a stray click, not a line: C3's cell envelope has two, 42 and 265 px away from the
  envelope contour of their section. (A fill already ignored them.)
- **A section without contours** is returned as zeros without rasterizing. The masks are the same; a whole
  plane of a 6-model class costs nothing on the sections no model reaches.

Everything else is as in `imod-model-mask`:

- **Grid.** `shape` is the image size in the model header (`xmax, ymax, zmax` → `(z, y, x)`): the size of the
  image the model was drawn on. Point coordinates are taken as pixel indices of that image, which is true when
  the model was drawn on the whole, unbinned stack. Smoke's label-vs-raw shape check is the guard: bind the
  model only to the raw it was drawn on.
- **Coordinates.** A point `(x, y, z)` lies on section `floor(z + 0.5)` (0-based, IMOD's own rounding). Pixel `i`
  covers `[i, i + 1)` in x. IMOD's y runs up from the bottom row of the image, array rows run down from the
  top, so a point's row coordinate is `ny - y`. Checked on EMPIAR-10554 by overlaying the masks on the raw
  TIFF planes (see the example).
- **`draw: fill`** (default). A pixel is 1 when its centre is inside an odd number of the object's contours
  on that section (even-odd rule), so a contour nested inside another is a hole, as in IMOD's meshing. Contours
  with fewer than 3 points are ignored.
- **`draw: outline`.** A pixel is 1 when a contour line passes through it (each segment sampled every half
  pixel; closed contours include the closing segment, open ones do not). Lines are one pixel wide; the snippet
  does not thicken them.
- **Several models.** With several URLs (e.g. one model per annotated cell of the same stack) each model is
  rasterized on its own and the mask is 1 where any of them is. All models must have the same header dims.
- **Access.** `file`: the 240-byte header is read at open; the first `read` reads the whole model (a few MB)
  and keeps the selected object's contours in memory. Later reads cost no bytes.
- **Voxel size.** Always `None`. A model's `pixsize`, `units` and `zscale` are typed in by the modeller for
  measurement and display and are not the image's calibration (10554's models say 3 nm, or 1 pixel, with z
  scale 6.47, against the TIFF's 8 × 8 × 20 nm).

## Params

- `object` (int): the 1-based object number, as 3dmod's object list and `imodinfo` show it. One binding entry
  per object, each a binary label. With several URLs the same number is used in every model.
- `objects` (str): for several URLs whose models number the same class differently: one number per URL, in
  the binding's URL order, e.g. `"6,1,2"`. Exactly one of `object` and `objects` is set.
- `draw` (str): `fill` or `outline`, above. Choose by what the class is: a contour drawn around a body
  (granule, vesicle, whole cell) is filled; a contour that traces a thin structure (a membrane) is that
  structure only as an outline, and filled it is the region the structure encloses.
- `open_contours` (str): `refuse`, `close` or `skip`; only with `draw: fill`. It applies to contours flagged
  open inside a closed object, never to an object that is open as a whole (that one has no inside anywhere and
  stays `outline` only). Leave it at `refuse` first: the error names the section, and whether the contour is a
  closed shape with a wrong flag (`close`) or a real open line among closed ones (`skip`) is read from its
  points, e.g. the distance between its ends against its length.

Object numbers and names are per model: read them from the model (`imodinfo`, or the `OBJT` chunks: 64-byte
name at the start of each) before binding, and say in `reasons` which name each number is. The opened array
carries the name it found as `object_name` after the first read.

## Known limits

- Only contours. Scattered-point objects are refused; meshes (`MESH` chunks) are skipped, so a mask is not
  interpolated between sections: a section with no contour is empty, even between two drawn sections.
- Contours must be planar in z (after rounding); a contour spanning sections is refused, as is a model with
  the flipped-y/z flag.
- `fill` refuses open objects, and open contours unless `open_contours` says otherwise. `close` joins the two
  ends with a straight line, whatever their distance.
- Contour flags other than "open" are ignored. In particular the snippet does not tell hand-drawn contours
  from ones IMOD's interpolator generated: in 10554's models most cell-envelope contours (e.g. 90 of 115 in
  C1) carry contour flag `0x20`, which looks like such a marker, so "manual" there may mean a few drawn
  sections and the rest interpolated.
- Objects that overlap (a granule inside a cell envelope) stay separate binary masks; the snippet builds no
  class-id or instance map. Contours of one object that are separate surfaces, and the cells of several
  models, are not numbered as instances.
- The `MINX` chunk (model-to-image transform) is not applied. A model saved on a binned or sub-area load of
  the image has other header dims and fails the shape check rather than being rescaled.
- ASCII models (`imod` text format) and older binary versions are refused.

## Example

`examples/empiar-10554-tuwongella-fib-sem-membranes.yaml`: the 464×2082×3720 FIB-SEM stack with its six
models (cells C1 to C6): three classes drawn in every model and five that only the damaged cell C1 has. The
example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
