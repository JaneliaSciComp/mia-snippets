# tiff-tiled-geojson-polygons

Reads page 0 of one **tiled TIFF or BigTIFF** at a plain HTTP(S) URL (S3 included), pyramidal or not, tile by
tile; and, as its label form, the **GeoJSON** polygons drawn on such an image, filled onto its pixel grid as
semantic ids. Made for whole-slide sections whose full-resolution page is far too large to decode whole
(64000 x 64000 x 3 = 12.3 GB).

## Raw form: `urls: [<one .tif>]`, no params

- **What is read.** Page 0 only, the full-resolution image. The other pages (the reduced-resolution levels of a
  pyramid) are never read. `axes` is `cyx` when the page has several samples per pixel (RGB: c = R, G, B) and
  `yx` when it has one; `shape` and `dtype` are the page's.
- **Access `chunk`.** At open only the TIFF header and page 0's tags are read. A read fetches the tiles the
  region touches, one byte range each, decodes them with tifffile's codec for the page (JPEG with the page's
  JPEGTables, and whatever else tifffile/imagecodecs decode) and keeps up to 256 MB of decoded tiles. A 256 x 256
  patch costs 1 to 4 tiles. A tile with a byte count of 0 reads as zeros. A server without Range support has the
  file staged whole (access `file`); do not use that on files of this size.
- **Voxel size** is `None`: TIFF resolution tags describe print size, and the tested files carry a default
  (10 px/cm).
- **Refused at open:** a page 0 that is not tiled (strips), has a depth, or stores its samples in separate
  planes.

## Label form: `urls: [<one .json or .geojson>, <its .tif>]`

The two urls may be in either order; the GeoJSON is recognised by its `.json`/`.geojson` ending. Only the TIFF's
header is read (for the grid), never its pixels. The GeoJSON is read whole on the first `read`.

- **The array** is `yx` `uint16` with page 0's shape. A pixel holds the `properties.data.id` (an integer from 1
  to 65535, the structure's ontology id) of the polygon that encloses the pixel's centre, 0 where none does.
  Several polygons may carry one id. Access `chunk`: a read fills only the part of each polygon inside the region.
- **Coordinate frame.** A GeoJSON position `[x, y]` is the page-0 pixel position (x, -y): x to the right from
  the left edge, y stored negated (GeoJSON's y points up, the image's down), an integer being a pixel corner.
  **The top-level `rotation` key is ignored**: the polygons fit the stored image without it, for both values
  met (see "Tested on").
- **Filling.** `Polygon` and `MultiPolygon`; rings are filled by the even-odd rule, so inner rings are holes.
- **Overlaps.** Polygons are drawn in order of decreasing area, so where two overlap the smaller one is kept (a
  structure outlined inside another shows; equal areas keep file order).
- **`skip_lines`.** `Point`, `LineString` and their Multi forms enclose no pixels. Such a feature is an error
  unless the binding sets `skip_lines: true`, which leaves it out. Say which features in `reasons`.
- **Errors:** a document that is not a FeatureCollection, any other geometry type, or a feature whose
  `properties.data.id` is missing, not an integer or outside 1..65535.

## Known limits

- No pyramid levels, no voxel size, no z or time; one TIFF per array.
- Tested on real data: BigTIFF, RGB uint8, 2000 x 2000 JPEG tiles (photometric RGB), Polygon features with one
  ring, `rotation` 270 and 90. Grey and uint16 pages, zlib tiles, edge tiles, holes, MultiPolygon and the id
  range check were run on synthetic files only.
- The `rotation` key was checked on two files only. A file whose polygons do need it would give a misplaced
  label; overlay a section before trusting a new specimen.
- A pixel centre exactly on an outline belongs to the polygon on its right/lower side; overlaps between
  neighbouring structures are resolved by area, not by any rule of the annotators.
- A crop on a huge page that touches four tiles fetches up to about 12 MB for 256 x 256 pixels; that is the
  file's tiling.
- A whole-plane label read allocates the plane (8 GB for 64000 x 64000); read regions.

## Tested on

`aws-dharani-fetal-brain-nissl-annotated-sections` (examples/), two annotated Nissl sections:

- `specimen_1/Specimen_1_43` (the record's sample; 461 MB, 6 pages, page 0 64000 x 64000; 8 Polygons of 4
  structures, `rotation` 270). Overlay on the 8000 px level: the outlines trace the pial surface and the layer
  boundaries. Against a tissue mask of the 2000 px level (darkest channel < 215) the union of the polygons has
  IoU 0.81 in the snippet's frame; 0.08 / 0.00 / 0.07 when also rotated 90 / 180 / 270 degrees about the image
  centre; 0.00 without the y negation or transposed; 0.76 and 0.75 shifted by 500 px in x or y.
- `specimen_5/Specimen_5_1066` (12.7 GB, 7 pages, page 0 80000 x 80000; 197 Polygons of 144 structures and 2
  LineStrings, `rotation` 90). Overlay on the 1250 px level: the filled polygons follow the cortical layers,
  ventricles and brainstem nuclei of the sagittal section; 37 % of the page is labelled. Same frame, IoU 0.46
  (pale tissue escapes the mask) against 0.19 to 0.34 for the rotated, unflipped and transposed frames.

`harness smoke` passes (4.4 MB fetched); `harness crop --n 4` made 4 crops from 64 MB, all on Specimen_5_1066.
The tissue of Specimen_1_43 covers about 1 % of its page, so random crops of it are empty slide.
