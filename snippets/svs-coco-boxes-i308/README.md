# svs-coco-boxes-i308

Reads one **Aperio SVS** whole-slide image as `yxc`, and, as its label form, the **bounding boxes** that an
**MS COCO JSON** holds for one slide, rasterised on that slide's pixel grid as filled boxes. Written for the
canine cutaneous mast cell tumor mitosis dataset (MITOS_WSI_CCMCT, Figshare collection 4552445), whose
`MITOS_WSI_CCMCT_MEL.json` holds 238,357 boxes for 32 slides and nothing else: `segmentation` is absent, so the
catalog's COCO readers (`svs-coco-polygons`, `tiff-coco-polygons`, `coco-json-polygons`), which read polygons,
refuse it, and the box readers take CSV, YOLO txt or XML.

Slides are 23k to 128k pixels on a side, so neither form builds the whole plane: a read costs what its region
costs.

## Raw form: `urls: [<one .svs>]`, no params

The reader of `svs-coco-polygons`, copied (snippets are independent).

- **What is read.** Page 0 of the TIFF, which in an SVS is the full-resolution level. The lower pyramid levels,
  the thumbnail, the label and the macro image are not presented. `axes` `yxc`, `c` = the file's samples in file
  order (R, G, B), `dtype` `uint8`.
- **Tile by tile.** A read seeks to and decodes (tifffile's `TiffPage.decode`, imagecodecs) only the tiles that
  intersect the region. A tile the file leaves empty (byte count 0) reads 0.
- **Voxel size.** `MPP = <µm per pixel>` of the page's Aperio description, times 1000, for `y` and `x`; `None`
  when the description has no positive `MPP`.
- **Access.** `chunk` when `io.open` gives a range-backed file: only the TIFF header, the tile offset tables and
  the touched tiles are fetched (in the harness's 1 MiB blocks). When `io.open` raises `NeedsFetch` the file is
  staged whole with `io.fetch` and access is `file`; reads still decode only the touched tiles.
- **Refused at open**, with the reason: any param; a TIFF whose first page is not an Aperio page; a page 0 that
  is not tiled, contiguous, `uint8` `YXS`; compression 33003 (Aperio JPEG 2000 with YCbCr samples), whose colour
  conversion was never tested here.

## Label form: `urls: [<COCO JSON>]`, `params: {file_name, boxes, bbox[, count]}`

The url is a JSON file, plain or a zip member (`<zip>::<member>`; a deflated member is fetched whole). The form
is chosen by the file's first bytes (`II`/`MM` = TIFF, `{` = JSON), not by its name. The array is `yx`.

- **`file_name`: which image.** It must equal the `file_name` of exactly one `images[]` entry. That entry's
  `height` and `width` are the `yx` shape, and the `annotations[]` whose `image_id` is its `id` are the label,
  in `annotations[]` order. Pair the label with the raw of that slide; the harness checks that the two shapes
  agree. An annotation of another image, or of none (`image_id` null: MITOS_WSI_CCMCT_MEL.json has 17, which
  have no category either), is not this image's and is not looked at.
- **`bbox`: what the four numbers are.** Only an annotation's `bbox` is read; `segmentation`, `area` and
  `iscrowd` are ignored. `xyxy` reads corners `[x1, y1, x2, y2]`, which is what the SlideRunner export of
  MITOS_WSI_CCMCT writes; `xywh` reads COCO's own `[x, y, width, height]`. The file does not say which it uses,
  so the param has no default and the binding says how it was told. Either way the numbers are pixels of the
  full-resolution image, x to the right and y down from the image corner, and the box covers the pixels
  `[y1, y2) x [x1, x2)`; a fractional edge is moved outwards (every pixel the box touches). A box that is empty
  or reversed raises and asks whether `bbox` is right: corners read as `xywh` do not fail by themselves (they
  give boxes as wide as their `x2`), so the binding has to check the choice on the numbers and the pixels.
- **Boxes over the edge are clipped** to the grid, and a box wholly outside it leaves no pixel (its instance
  number stays unused). The grid is the same file's `width` and `height`, so such a box is a stray annotation,
  not a sign of a wrong pairing: MITOS_WSI_CCMCT_MEL.json has one among its 238,340 boxes with an image.
- **`boxes: instance`** (`uint32`): each box is filled with its 1-based number among the image's annotations,
  in `annotations[]` order (not the annotation's `id`, which runs over all images). `category_id` is not looked
  at. **`boxes: semantic`** (`uint8`, or `uint16` when an id used is above 255): each box is filled with its
  `category_id`, the file's own id, not renumbered. An annotation of the image whose `category_id` is not a
  positive whole-number `id` of `categories[]` raises: a box without a class has no semantic value, and 0 is
  background.
- **Overlap.** Background is 0. Boxes are painted largest first, so where two overlap the smaller one is on top;
  among boxes of one size (all of MITOS_WSI_CCMCT's are 50 x 50) the later one in `annotations[]` is on top. A
  box lying wholly under others leaves no pixel.
- **`count`**: the number of annotations the image must have, so a changed file fails at open instead of
  renumbering the instance ids.
- **The labels are boxes, not masks**: where a box was drawn, here a fixed square around a cell's centre, not
  the cell's outline. Whether a binding may call that `instance` or `semantic` is a human's decision (see the
  example binding's `reasons`).
- **Voxel size** is `None`: COCO states none. The paired raw has it.
- **Access `chunk`.** The whole JSON is read and parsed once at open (the shape is in it; the 20.8 MB
  MITOS_WSI_CCMCT_MEL.json takes about a quarter of a second to parse); only the boxes of the one image are
  kept. A read paints only the boxes that meet its region and costs no bytes.
- **Refused at open**, with the reason: a missing or unknown `file_name`, `boxes` or `bbox`; no or several
  `images[]` entries with the `file_name`; an image entry without whole-number `id`, `height` and `width`; a
  `bbox` that is not four finite numbers, or is empty or reversed; `count` not met; for `semantic`, a
  `category_id` that is not a positive id of `categories[]`.

## Known limits

- Tested on one slide of one dataset: JPEG 2000 tiles with RGB samples (compression 33005), 240x240, and its
  2,891 `xyxy` boxes. `xywh`, clipping, fractional edges, zip-member fetch and ids above 255 were tested on
  synthetic files only. The other 31 slides of the JSON open in both modes (their boxes were not laid over
  their pixels: the record has no URL for those slides).
- An image without annotations is an all-zero label, not an error; `count: 0` states that it is meant.
- A read with a slice step builds the region's bounding box first, so a strided read of a whole slide needs the
  whole plane in memory. The harness reads boxes.
- One image per array. Binding all slides of a dataset means one raw and its label entries per slide; each
  label parses the JSON again at open (from the cache after the first).
- The grid is the JSON's `width` x `height`; the snippet does not open the slide to compare. The harness does,
  through `pairs_with`.
- Polygons and RLE masks are not read; for polygons use `svs-coco-polygons`.

## Example

`examples/figshare-c-4552445-canine-mast-cell-tumor-mitosis.yaml`: the record's sample slide
(`2efb541724b5c017c503.svs`, 23399 x 76964, H&E, 0.2533 µm per pixel) as the raw, and its 2,891 cell boxes out of
`MITOS_WSI_CCMCT_MEL.json` twice: by class (ids 1..5 of the file's 8 categories occur on this slide) and by
instance. While testing, the boxes were laid over the slide: each frames a nucleus (the mitotic-figure boxes a
mitotic figure), and the stain at the box centres is much stronger than at the same coordinates flipped,
mirrored or read as `xywh`. The example binding's `snippets_commit` is the commit before this snippet's own (a
binding can't name the commit that adds it); a binding written by the worker names a commit that contains the
snippet.
