# geojson-polygons-tiff-grid

Reads the polygons of one **GeoJSON** file, as QuPath exports objects, as a label on the pixel grid of the
**TIFF** image they were drawn on: instance ids (one per feature) or a binary mask.

## Urls and what is read

`urls: [<the .geojson or .json>, <its TIFF>]`, in any order: exactly one url whose path ends in `.geojson` or
`.json`, and exactly one other, which must be a TIFF (OME-TIFF and BigTIFF included).

- **The TIFF gives the grid and nothing else.** A GeoJSON file holds coordinates only, so the shape is the
  ImageLength x ImageWidth of the TIFF's first page, and the voxel size is the OME-XML's `PhysicalSizeX/Y` (else
  ImageJ's unit and resolution, else `None`). Only that page's header is read by range, about 1 to 3 MiB whatever the
  file's size; no pixel of the TIFF is read. Bind the raw of the same file with a TIFF snippet.
- **The GeoJSON is fetched whole on the first `read`** (`io.fetch`; nothing but its size is asked for at open) and
  decoded feature by feature, so a file of 100 MB costs its own size once and little memory beyond its text. It may be a
  `FeatureCollection`, a bare list of features (QuPath writes both), or a single `Feature`.
- **The array** is `yx`, `access` `plane`. After the first read any region is filled from the polygons whose
  bounding boxes touch it; a whole 4493 x 4720 plane of tens of thousands of cells takes seconds.

## How polygons become pixels

- **Coordinate frame.** Coordinates are pixels of the paired image, x to the right and y down, an integer
  coordinate being a pixel corner (QuPath's convention: pixel (col, row) covers `[col, col+1) x [row, row+1)`).
  A pixel belongs to a feature when its **centre** lies inside the feature's polygon.
- **Even-odd rule over all rings of a feature**, so a `Polygon`'s holes are holes and a `MultiPolygon` is the
  union of its parts. Rings are closed whether or not the file repeats the first point.
- **Instance form (default):** `uint32`; a pixel holds the feature's **1-based position in the file** (counting
  every feature, kept or not, so an id names a feature and ids are not consecutive after filtering). Where two
  features overlap, the later one in the file is kept. 0 where no kept feature covers the pixel.
- **Binary form (`binary: true`):** `uint8`, 1 where any kept feature covers the pixel.
- **Frame check.** If the kept polygons reach more than 1 pixel beyond the TIFF's grid, the first read fails:
  the coordinates are then not this image's pixels (a wrong pairing, or coordinates of a larger mosaic).

## Params

| param | meaning |
|---|---|
| `member` | the feature key that holds the polygon. Default `geometry`. QuPath cell objects also carry `nucleusGeometry`; a feature without the named key is left out (its id stays unused). |
| `binary` | `true` for the mask form, for region annotations such as a tumour area. |
| `keep_classes` | list of QuPath classification names (`properties.classification.name`); only those features are filled. `null` in the list stands for unclassified features. |
| `drop_classes` | list of classification names left out. Use this rather than `keep_classes` when other files of the dataset may hold classes not seen yet. |
| `empty_ok` | `true` reads a GeoJSON of 0 bytes as an all-zero label. Without it such a file is an error at open: an empty file does not say that the image holds no object, so the default is to leave it out of the binding. A valid collection with no features (`"features": []`) does say so and is read as all zero without the param. |

If the class params leave no feature of a file that has some, or no kept feature has the `member`, the read
fails and names the classifications the file holds, so a misspelt class does not become an empty label.

## Known limits

- Only `Polygon` and `MultiPolygon` are filled. A kept feature with any other geometry (point, line, geometry
  collection) is an error; leave such features out by their classification.
- QuPath's own labelled-image export rounds polygon edges with Java2D; the fill here (pixel centre inside) can
  differ from it on outline pixels only.
- Classification names are matched exactly; derived classes ("A: B") are one name.
- Object hierarchy, measurements, names and colours in `properties` are ignored. Semantic ids per class are not
  produced: one binding entry per class with `binary: true` and `keep_classes` gives one mask per class.
- The whole GeoJSON text and the edges of the kept polygons are held in memory after the first read (for an
  80 MB file a few hundred MB).
- The parts not run on real data: `MultiPolygon` in the binary form, bare-list and single-feature files, holes,
  `keep_classes`, `empty_ok` and the ImageJ voxel size were run on synthetic input only, compared pixel for pixel with
  a brute-force point-in-polygon test.
