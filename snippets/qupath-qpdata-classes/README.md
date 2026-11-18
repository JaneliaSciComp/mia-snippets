# qupath-qpdata-classes

Reads the annotations of one **QuPath image data file** (`data.qpdata`, the per-image file inside a QuPath
project's `data/<entry id>/` folder) and fills them onto the image's full-resolution pixel grid as a `yx`
`uint8` map of class ids. The url is a zip member (`<zip>::<entry>/data.qpdata`) or a plain HTTP(S)/S3 URL.

A `.qpdata` file is a Java object serialization stream (magic `aced0005`). No Java, QuPath or serialization
library is on the harness allowlist, and `pickle`-like loaders are banned, so `snippet.py` walks the stream
grammar itself with `struct`. Nothing is instantiated: an object is kept as its class names, its field values
and the delimited data its class wrote.

## What is read

- **The stream**, as QuPath's `PathIO` writes it: a version line, a JSON string describing the image server,
  the locale, image type, stains and workflow, then the `PathObjectHierarchy`. Reading stops after the
  hierarchy. QuPath's path objects are `Externalizable` and written in block-data mode, so each object's content
  is delimited in the stream: the snippet takes, from each `PathAnnotationObject`, the nested `PathClass`, the
  nested ROI and the nested child objects, without relying on their positions.
- **The grid** is the one the file states for its image: `server.metadata.width` x `height` of the JSON string,
  the level-0 size of the slide the annotations were drawn on. `shape` is `(height, width)`. An image with
  `sizeZ` or `sizeT` other than 1 is refused. `image_name` (not part of the Array protocol) holds
  `server.metadata.name`, the slide's file name, for whoever checks the pairing.
- **Voxel size** from `server.metadata.pixelCalibration` (`pixelHeight`, `pixelWidth`) when both are in µm,
  times 1000; otherwise `None`.
- **ROIs.** `PolygonROI` (its `x`/`y` float vertex arrays) and `GeometryROI` (its WKB bytes: Polygon,
  MultiPolygon, or a GeometryCollection of them, either byte order, with holes). Any other ROI type on an
  annotation of a listed class (rectangle, ellipse, line, polyline, points, the legacy `AreaROI`) is refused
  at open with its name; on an unlisted class it is not looked at.
- **Rasterization.** ROI coordinates are QuPath pixel coordinates: x right, y down, an integer coordinate being
  a pixel corner. A pixel belongs to an annotation when its centre `(x + 0.5, y + 0.5)` is inside, by the
  even-odd rule over the rings of a polygon (so a hole ring is a hole); the polygons of a multipolygon are
  united. A centre exactly on an edge is inside on the left and top edges and outside on the right and bottom
  ones. Parts of an annotation outside the image are cut off.
- **Only annotation objects** (`PathAnnotationObject`), wherever they sit in the hierarchy. Detections, cells,
  tiles and TMA cores are walked for their children but not drawn.
- **Access `file`.** The file (tens of kB to a few MB) is read whole at open, by range, or staged with
  `io.fetch` when it is a deflated zip member; open also parses it and checks `classes`. `read` only rasterizes:
  the annotations whose bounding box meets the region, in strips of at most 16 megapixels.

## `classes` (required)

Comma-separated QuPath class names, in id order: `classes: "Positive,Negative"` gives 1 where a `Positive`
annotation is, 2 where a `Negative` one is and 0 elsewhere. One name gives a `{0,1}` mask of that class.

- Names are matched exactly as QuPath shows them, `*` included (`Ignore*`, `Region*`); a derived class is
  `Parent: Child`. A name cannot contain a comma. Spaces around a name are dropped.
- Classes are painted in the order listed, so **where annotations of two listed classes overlap, the pixel
  holds the class listed later**. List an enclosing class (e.g. a tissue outline) before the classes drawn
  inside it.
- Annotations of unlisted classes and unclassified annotations are left out: they read 0, the same as "not
  annotated".
- A listed class that has no annotation in this file is fine (its id is just absent, so ids stay the same
  across the files of a project), but **if no annotation has any listed class the open fails** and the error
  lists the classes the file does have with their counts. That is how a misspelt name shows.
- The ids are the binding's choice, not the file's: a `.qpdata` has class names and colours, no class numbers.
  Say in `reasons` which classes were taken, why in that order, and what the unlisted ones are.

Take the names from the project's `summary.json` beside the `.qpdata` (`annotationClassificationCounts`), or
from the error above.

## Known limits

- Tested on real data: QuPath 0.5.1 files (data file version 3, stream protocol 2) with flat hierarchies of
  `PolygonROI` and `GeometryROI` annotations, classes without parents; the geometries are big-endian WKB
  Polygons with up to 14 rings and MultiPolygons of up to 36 polygons. All 42 `.qpdata` files of the tested
  record's `Data.zip` were parsed (768 annotations: 736 polygons, 32 geometries), and for every annotation the
  filled pixel count agrees with the area QuPath stored with the ROI (difference below 0.09 px per px of
  perimeter). Nested hierarchies, derived classes, GeometryCollections, little-endian and 3D WKB were run on
  synthetic input only.
- Data files older than version 3 have no image-server JSON, so the grid is unknown and the open fails.
  A stream written with protocol 1 (no block data for Externalizable objects) cannot be read without the classes
  and is refused.
- A self-intersecting polygon is filled even-odd; QuPath may treat such an outline differently.
- The label is as large as the slide: reading the whole plane of a 100000 x 80000 slide allocates 8 GB. Boxes are
  cheap.
- Annotations are whatever the project's author drew: sparse training regions stay sparse, and 0 does not mean
  background. `locked`, names, measurements and colours are not read. A ROI's z/t/c plane is ignored (the image
  must be a single plane).

## Tested on

`zenodo-23535439-hamster-lung-sars-cov-2-ihc-wsi`: slide `115-23-47.svs` (read with `tiff-zip-member-2`) and
`Data.zip::43/data.qpdata`, `classes: "Positive,Negative"`. Label `yx` 19867 x 9566, 262.238 nm; 24 Positive
polygons (15554 px) and 2 Negative ones (13735 px). The coordinate frame was checked against the slide: the mean
RGB inside the Positive polygons is 110/31/22 (DAB brown) and 205/179/150 with the polygons moved 40 px either
way in x or y; inside Negative it is 225/226/244 (hematoxylin only), inside `Ignore*` 252/252/251 (white).
