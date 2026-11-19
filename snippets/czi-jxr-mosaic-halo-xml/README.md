# czi-jxr-mosaic-halo-xml

Reads one tiled **Zeiss CZI** mosaic at a plain HTTP(S) URL (S3 included) as a stitched `cyx` image, **JPEG XR**
tiles included; and, as its label form, the polygons of a **HALO `.annotations`** XML file filled onto the pixel
grid of such a mosaic as semantic ids.

The raw form is `czi-mosaic-imagej-roi`'s CZI reader with JPEG XR decoding added (that snippet refuses such
tiles). It is a new snippet, not a successor: the ImageJ ROI label form is not carried over. No CZI or HALO
library is on the harness allowlist, so `snippet.py` parses both itself: the `ZISRAWFILE` header, the
`ZISRAWDIRECTORY` subblock directory, the `ZISRAWSUBBLOCK` segments of the tiles a read touches and the
`ZISRAWMETADATA` XML for the pixel size (`struct`); and the HALO XML (`xml.etree`).

## Raw form: `urls: [<one .czi>]`, no params

- **What is read.** Every full-resolution subblock (tile) of the file, placed on one canvas at the X and Y start
  the subblock directory gives it, minus the smallest X and Y start of all tiles. `axes` `cyx`; `c` is the
  directory's C index in ascending order (the order of the XML's channel list); `shape` is the bounding box of
  the tiles; `dtype` is the tiles' pixel type (Gray8 `uint8`, Gray16 `uint16`, Gray32Float `float32`).
- **Stitching is placement only.** Nothing is registered, blended or resampled. Where tiles overlap, the pixel of
  the tile with the higher M (mosaic) index is returned, the order in which libCZI's tile accessor draws them. A
  pixel no tile covers reads 0.
- **Voxel size.** `Metadata/Scaling/Items/Distance[@Id=X|Y]/Value`, which CZI states in metres, times 1e9. `None`
  when the file has no such element.
- **Access `chunk`.** A read fetches and decodes only the tiles that intersect the region, each by one byte
  range, and keeps up to 256 MiB of decoded tiles. At open only the header, the directory and the XML are read
  (4 MiB for the tested 8.1 GB file). A server without Range support is staged whole (access `file`).
- **Compression.** JPEG XR (each subblock is one JXR file, decoded with `imagecodecs.jpegxr_decode`; it must
  decode to the tile's size and pixel type), uncompressed, zstd0 (one zstd frame) and zstd1 (a small header,
  then one zstd frame; its flag for 16-bit pixels stored as low bytes then high bytes is honoured). JPEG XR is
  lossy as ZEN writes it: the values are the decoded ones, not the camera's.
- **Refused at open**, with the reason: tiles that span more than one scene, z plane, time point or any dimension
  other than X, Y, C and M; JPEG or LZW tiles; colour or complex pixel types, or channels of different pixel
  types; a multi-part file. Pyramid subblocks (stored size smaller than the extent they cover) are skipped.

## Label form: `urls: [<one .annotations>, <its .czi>]`, `params: {layers: [<name>, ...]}`

The urls are exactly one HALO annotation file (recognised by the `.annotations` ending of the url path) and
exactly one CZI, in any order. Only the CZI's header, directory and XML are read (for the shape and the voxel
size), never its pixels.

- **The file** is `<Annotations><Annotation Name="..."><Regions><Region Type="Polygon" NegativeROA="0|1">
  <Vertices><V X=".." Y=".."/>`: named layers of polygons, some of them marked negative.
- **`layers`** (required) names the layers to fill, by `Annotation/@Name`. **The id of a pixel is the 1-based
  position in `layers` of the layer it lies in**, 0 when it lies in none; where two listed layers overlap, the one
  later in the list is kept. A layer that is not listed is not read. A listed name the file does not have is an
  error that lists the file's names. Choose the layers that are classes and leave out layers that mark a region
  of interest, and say which is which in `reasons`.
- **The array** is `yx` `uint8` with the CZI mosaic's y and x shape and voxel size.
- **Holes.** A pixel lies in a layer when more of the layer's positive regions than of its negative
  (`NegativeROA="1"`) regions enclose the pixel's centre. So a negative region is a hole in the positive region
  around it, a positive region inside a hole is an island, overlapping positive regions are their union, and a
  negative region outside every positive one changes nothing. Each polygon is counted once whatever the
  direction it was drawn in.
- **Coordinate frame.** Vertex `X`, `Y` are taken as pixel coordinates of the stitched mosaic as the raw form
  returns it: x to the right and y down from the corner of the canvas, an integer coordinate being a pixel
  corner. The `.annotations` file does not say which image or frame it was drawn in; see "Tested on" for how
  this was checked. The check was made on a file whose tiles start at 0, 0, where the canvas corner and the CZI's
  own origin are the same point, so **a CZI whose tiles do not start at 0, 0 is refused** in the label form
  rather than guessed at.
- **Access `chunk`.** The annotation file is fetched whole on the first `read` (25 MB to 177 MB in the tested
  dataset) and its polygons are kept as edges (1.4 s for 660,000 vertices). After that a read rasterises only
  the edges that cross its rows, so its cost follows the region, not the slide.
- **Refused**, with the reason: a listed layer with a region whose `Type` is not `Polygon` (HALO also writes
  rectangles and ellipses) or whose `NegativeROA` is neither 0 nor 1; `layers` missing, empty, repeated or longer
  than 255; `layers` given without an `.annotations` url. A polygon with fewer than 3 vertices or no area fills
  nothing.

## Known limits

- Tested on real data: JPEG XR Gray16 tiles on a grid without overlap, one scene, 5 channels x 1598 tiles;
  integer-coordinate polygons with holes and islands. The uncompressed, zstd0, zstd1, Gray8 and Gray32Float
  paths are `czi-mosaic-imagej-roi`'s code unchanged and were not run again here; non-integer vertices and
  overlapping positive regions were run on synthetic input only.
- Tiles are placed at integer pixel positions from the directory. ZEN's own stitching is not reproduced; a file
  that needs it shows seams in the overlaps.
- A polygon that crosses itself is filled by winding number, not by the even-odd rule: a loop drawn against the
  direction of the rest of the outline counts as outside. Classifier output does not cross itself; a
  hand-drawn outline can (the unbound `Layer 1` of the tested file has 641 such pixels in 66 million sampled).
- Two overlapping positive regions that share one hole keep the hole filled (2 - 1 > 0).
- Whether HALO puts an integer vertex on a pixel corner or a pixel centre is not known; the label can be half a
  pixel off, which is below what the frame check resolves.
- Scenes are not separated: a multi-scene file is refused rather than read as one huge canvas.

## Tested on

`s-biad1237-lung-adenocarcinoma-mif-wsi` (examples/): `iCAIRD_L122.czi` (8.1 GB), 17,495 subblocks of which
7,990 are full-resolution (5 channels x 1,598 tiles of 2236 x 2236, the last column 1442 wide and the last row
1957 high; Gray16, JPEG XR, 14 significant bits) on a grid without overlap that starts at 0, 0, stitched to
5 x 75745 x 104298, 227 nm per pixel, the SizeX/SizeY the file's XML states; and `iCAIRD_L122.annotations`
(25 MB): `Layer 1` 3 polygons (2 negative), `Stroma` 2,558 (1,387 negative), `Tumour` 3,235 (1,088 negative),
integer vertices. Bound with `layers: [Stroma, Tumour]`.

**Hole rule**, on 24 bands of 32 rows across the annotated area (66 million pixels), counting positive and
negative cover per layer: tumour lies in holes of stroma (1.7 M pixels) and stroma in holes of tumour (94 k);
103 k pixels are stroma islands inside stroma holes, which a rule of "any negative region excludes" would
drop; 29 pixels are in both layers. So with the rule above the two layers do not overlap.

**Frame check** on two 1024 x 1024 windows holding both classes (2.1 M labelled pixels), as the ratio of the
mean intensity inside Tumour to that inside Stroma:

| label position | DAPI | Fcein | Cy3 | Cy5 | AF750 |
|---|---|---|---|---|---|
| as the file states | 1.28 | 0.92 | 1.70 | 1.52 | 3.68 |
| moved 15 px (4 directions) | 1.27 to 1.28 | 0.91 to 0.92 | 1.68 to 1.71 | 1.49 to 1.52 | 3.50 to 3.56 |
| moved 40 px | 1.24 to 1.26 | 0.89 to 0.93 | 1.62 to 1.68 | 1.43 to 1.46 | 2.90 to 3.13 |
| moved 120 px | 1.13 to 1.17 | 0.88 to 1.00 | 1.33 to 1.47 | 1.19 to 1.28 | 1.75 to 1.94 |
| moved 240 px | 0.99 to 1.11 | 0.91 to 1.06 | 1.03 to 1.20 | 1.03 to 1.06 | 1.14 to 1.23 |
| x, y or both mirrored | 0.95 to 1.10 | 0.95 to 1.12 | 0.84 to 1.12 | 0.86 to 1.09 | 0.82 to 1.23 |

The contrast is highest at the stated position and falls with every shift, so the polygons sit on the tissue
they name in the stitched-mosaic frame, with no flip and no offset of the order of 15 pixels (3.4 um). A
transpose is ruled out by the extents alone: vertex X reaches 94,249 and the mosaic is 75,745 high.

The fill itself was compared with a per-polygon even-odd fill on synthetic layers (integer and float vertices,
both drawing directions, a hole, an island in the hole, an overlapping second region, a second layer): equal on
every pixel, also for sub-regions, strided reads and banded rasterising.
