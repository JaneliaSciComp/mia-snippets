# svs-coco-polygons

Reads one **Aperio SVS** whole-slide image as `yxc`, and, as its label form, the polygons that an **MS COCO
JSON** holds for one slide, rasterised on that slide's pixel grid as a semantic mask of category ids. Written for
CATCH (TCIA), where one `CATCH.json` holds the polygons of all 350 slides.

Slides are 50k to 120k pixels on a side, so neither form builds the whole plane: a read costs what its region
costs.

## Raw form: `urls: [<one .svs>]`, no params

- **What is read.** Page 0 of the TIFF, which in an SVS is the full-resolution level. The lower pyramid levels,
  the thumbnail, the label and the macro image are not presented. `axes` `yxc`, `c` = the file's samples in file
  order (R, G, B), `dtype` `uint8`.
- **Tile by tile.** A read seeks to and decodes (tifffile's `TiffPage.decode`, imagecodecs) only the tiles that
  intersect the region. A tile the file leaves empty (byte count 0) reads 0.
- **Voxel size.** `MPP = <µm per pixel>` of the page's Aperio description, times 1000, for `y` and `x`; `None`
  when the description has no positive `MPP`.
- **Access.** `chunk` when `io.open` gives a range-backed file: only the TIFF header, the tile offset tables and
  the touched tiles are fetched (in the harness's 1 MiB blocks). When `io.open` raises `NeedsFetch` (the server
  states no size or ignores Range) the file is staged whole with `io.fetch` and access is `file`; reads still
  decode only the touched tiles.
- **Refused at open**, with the reason: a TIFF whose first page is not an Aperio page; a page 0 that is not
  tiled, contiguous, `uint8` `YXS`; compression 33003 (Aperio JPEG 2000 with YCbCr samples), whose colour
  conversion was never tested here.

## Label form: `urls: [<COCO JSON>]`, `params: {file_name: <slide file name>}`

The url is a JSON file, plain or a zip member (`<zip>::<member>`; a deflated member is fetched whole). The form
is chosen by the file's first bytes (`II`/`MM` = TIFF, `{` = JSON), not by its name.

- **Which image.** `file_name` must equal the `file_name` of exactly one `images[]` entry. That entry's `height`
  and `width` are the `yx` shape, and the `annotations[]` with its `id` as `image_id` are the label. Pair the
  label with the raw of that slide; the harness checks that the two shapes agree.
- **Values.** Each pixel holds the `category_id` of the polygon that covers it, 0 where none does. `uint8` when
  the ids used are at most 255, else `uint16`. The ids are the file's own, not renumbered.
- **Polygons.** `segmentation` is either one flat list `[x0, y0, x1, y1, ...]` (what CATCH.json holds, although
  COCO specifies a list of such lists) or a list of such lists, whose rings are then filled together. Vertices
  are pixel coordinates of the full-resolution image, x to the right, y down, the image corner at (0, 0); a
  pixel `(y, x)` is inside when its centre `(x + 0.5, y + 0.5)` is, by the even-odd rule. Vertices outside the
  image are allowed (CATCH has some at negative y); the polygon is clipped by the grid.
- **Overlap rule.** Where polygons overlap, the pixel gets the category of the **smallest** one (polygons are
  painted in order of falling area, the sum of their rings' shoelace areas; equal areas in `annotations[]`
  order). This is the reading for nested outlines, a small structure drawn inside a large region. COCO states no
  drawing order, so this is the snippet's choice; the example binding says how it was checked.
- **Voxel size** is `None`: COCO states none. The paired raw has it.
- **Access `chunk`.** The whole JSON is read and parsed once at open (the shape is in it; CATCH.json is 60 MB,
  about one second); only the polygons of the one image are kept. A read fills only the polygons whose bounding
  box meets the region, and only inside the region.
- **Refused at open**, with the reason: no or several `images[]` entries with the `file_name`; an annotation of
  that image with `iscrowd` set or an RLE `segmentation`; a ring with fewer than three vertices or an odd number
  of coordinates; a `category_id` that is not a positive integer id of `categories[]`.

## Known limits

- Tested on one slide of one dataset: JPEG 2000 tiles with RGB samples (compression 33005), 240x240. Other
  compressions that tifffile decodes (JPEG, the most common in SVS) take the same path but were not tested on
  real data.
- A read with a slice step builds the region's bounding box first, so a strided read of a whole slide needs the
  whole plane in memory. The harness reads boxes.
- An image without annotations is an all-zero label, not an error.
- Binding all slides of a dataset means one label entry per slide; each parses the JSON again at open.
