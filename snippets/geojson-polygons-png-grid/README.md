# geojson-polygons-png-grid

Reads GeoJSON polygon annotations as label planes: one GeoJSON file per image, one file per z plane (no z axis
for a single file), painted on the pixel grid of the PNG image the file annotates. It is for QuPath-style
exports, a `FeatureCollection` whose features are `Polygon` or `MultiPolygon` outlines in the image's own pixel
coordinates, with the class in `properties.classification.name`. It reads labels only; the PNGs themselves are
read with `image-tiles-glob`.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are concatenated in
  binding order, and a file named twice raises. Plane k pairs with plane k of a raw bound by the sibling glob
  (`patch-geojsons/*.geojson` and `patches/*.png`) when both directories hold the same stems. The snippet does
  not see the raw array, so the binding's `reasons` must say why the names pair.
- **`image`** (required). The PNG that gives a file's grid, relative to the GeoJSON's directory, with one `*`
  for the GeoJSON's name without directory and extension: `../patches/*.png` maps
  `patch-geojsons/<stem>.geojson` to `patches/<stem>.png`. `..` steps up inside the zip (or below the host of a
  plain URL); a pattern that leaves it raises. Only the PNG header (IHDR width and height) is used. Every plane's
  PNG must have the first file's size; one that differs raises at `read`.
- **Rasterisation.** Coordinates are x, y in pixels with the origin at the image's top-left corner and pixel
  (row r, column c) covering x in [c, c+1), y in [r, r+1): QuPath's convention, so a vertex may lie on the right
  or bottom edge (x = width). A pixel belongs to a feature when its centre (c + 0.5, r + 0.5) is inside the
  outline by the even-odd rule over all the feature's rings, so inner rings are holes and the parts of a
  `MultiPolygon` share one id. Rings are closed whether or not the file repeats the first position. Where
  features overlap, the one covering fewer pixels is painted on top (file order on a tie).
- **Ids.** Without `classes`: instance ids, each feature's 1-based number in its file's `features` list (ids
  start again at 1 in every plane; a feature that covers no pixel centre leaves its number unused). With
  **`classes`**: semantic ids, the 1-based position of the feature's `properties.classification.name` (or a
  plain string `classification`) in the list; a class that is not listed, or a feature without one, raises. 0 is
  background. A file with an empty `features` list is an all-background plane.
- **Refusals** (`ValueError`). A file that is not a JSON `FeatureCollection`; a feature whose geometry is not
  `Polygon` / `MultiPolygon` (points and lines enclose no pixels); a ring with fewer than 3 positions or
  non-finite coordinates; a feature with a vertex outside `[0, width] x [0, height]` of its PNG, which means the
  coordinates are not that image's pixels (e.g. whole-slide coordinates); an `image` that is not a PNG.
- **Axes / dtype.** `zyx` (or `yx`), `uint16`. **Voxel size:** `None`; GeoJSON carries none.
- **Access.** `plane`: any region of a plane costs its whole GeoJSON plus its PNG's header. A stored member or
  a Range-capable URL is read through `io.open` (33 bytes of the PNG); a deflated member raises `NeedsFetch` and
  is staged whole with `io.fetch`, so the PNG header of a deflated member costs the whole PNG once, the same
  cache entry the raw array's read of that plane uses. `open_array` reads the first GeoJSON and its PNG's header
  only; the last painted plane is kept.

## Known limits

- The grid comes from a PNG only (not JPEG or TIFF), and the outlines must already be in that image's pixels: no
  offset, scale or downsample factor is applied.
- Only `properties.classification` is read. Other QuPath properties (`objectType`, `name`, measurements) and the
  feature `id` are ignored, so annotations and detections are painted alike.
- `GeometryCollection`, points and lines raise instead of being skipped.
- Instance ids are per file, not unique across planes. More than 65,535 features in one file raise.
- A polygon thinner than a pixel can miss every pixel centre and paint nothing.
