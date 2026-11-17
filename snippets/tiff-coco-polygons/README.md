# tiff-coco-polygons

Reads one plain 2D **TIFF** (the image), and, as its label form, the **MS COCO** polygon annotations of that
image filled onto its pixel grid as semantic class ids. One COCO JSON usually annotates many images, so the
label form names the image it is for.

## Raw form: `urls: [<one .tif>]`, no params

- **What is read.** Series 0, level 0 of the file with tifffile: the full-resolution page of a pyramidal TIFF
  (reduced-resolution pages are not presented), or the only page of a flat one. Grey is `yx`, RGB is `yxc`
  (`cyx` when the samples are stored as separate planes); dtype as stored.
- **Refused at open:** more than one series, a series of several pages (stacks, channels as pages), axes other
  than Y, X and samples. Those are for `tiff-zip-member-2` and the other `tiff-*` snippets.
- **Voxel size** is `None`. Plain TIFF resolution tags are not read: scanner exports leave defaults in them
  (10 px/cm in the tested file, whose record states 4 µm per pixel).
- **Access `plane`**: a read decodes the whole page (`file` for a deflated zip member or a server without Range,
  which is staged whole).

## Label form: `urls: [<the COCO .json>]`, `params: {image: <file_name>}`

- **`image`** is the `images[].file_name` of the image the label is for. Exactly one entry must have it. The
  array is `yx` with that entry's `height` x `width`; the harness then checks it against the paired raw.
- **Values.** A pixel holds the `category_id` of the polygon that encloses its centre, 0 where none does.
  Ids are the file's own, not renumbered, so they need not be consecutive. `uint8` when every category id the
  file defines is at most 255, else `uint16`. Category names are not used; take them from `categories`.
- **Overlap rule.** Polygons are drawn largest first (by the shoelace area of their outlines), so of
  overlapping polygons **the smaller is on top**; of two equal areas, the later in the file. A region annotated
  inside a larger one (necrosis inside a tumour) therefore stays visible whatever the file order.
- **Coordinates** are image pixels, x right and y down, an integer coordinate being a pixel corner (COCO's
  convention). Each outline is filled by the even-odd rule; a `segmentation` with several outlines is their
  union. Both the COCO form `[[x0, y0, x1, y1, ...], ...]` and one flat list `[x0, y0, x1, y1, ...]` (what the
  tested file holds) are read.
- **`clip`.** A polygon with a vertex outside the `width` x `height` grid is an error unless the binding sets
  `clip: true`, which cuts it at the border. The error is the default because such polygons can mean a frame
  problem (see Known limits), and a misplaced label is worse than none.
- **Refused at open:** RLE segmentations and `iscrowd` annotations, a polygon of fewer than 3 points, an
  annotation whose `category_id` the file does not define, category ids outside 1..65535.
- **Access `file`.** The JSON is staged whole and parsed at open, because the shape is in it (30 MB for the
  tested file); only the named image's polygons are kept. A read fills only the polygons whose bounding box
  meets the region, by scanline, so a crop costs milliseconds.
- **Voxel size** is `None`: COCO states none.
- Boxes (`bbox`), keypoints and the `area` field are ignored. An image without annotations is all 0.

## Known limits

- The fill is by pixel centre. pycocotools and OpenCV (`drawContours` on integer coordinates, which the
  dataset's authors use) round outlines differently; the difference is confined to outline pixels.
- Semantic only: two touching polygons of one class become one region. Instance ids per polygon are not
  offered.
- **Other scanners of the tested dataset are not verified, and P1000 looks offset.** In `scc.json`, polygons
  leave the stated `width` x `height` in 39 of the 44 `*_p1000.tif` images (all listed as 4482 x 10859), by
  hundreds of pixels (for `scc_08_p1000.tif`, x reaches 5106 and y 11666), and in 8 `gt450`, 2 `nz210` and
  1 `nz20` images by up to about 200 pixels. No `cs2` image has any. The dataset's annotations were drawn on
  one scan and carried to the others, so small overshoots may be tissue outside a scan's field of view, but the
  P1000 numbers suggest polygons in another frame than the TIFF. The snippet refuses these images without
  `clip: true`; before setting it, overlay the label on the image. Zenodo's rate limit (429) stopped the check
  of `scc_08_p1000.tif` here.

## Tested on

`zenodo-7418555-multiscanner-canine-scc-histology` (examples/): `scc_08_cs2.tif`, a deflate-compressed
128 x 128-tiled pyramidal RGB BigTIFF of 7 pages, read as `yxc` 4338 x 5624 x 3 `uint8`; and `scc.json` (220
images, 6,215 polygons of 3 to 8,671 points, all integer-valued, flat-list `segmentation`), of which the 11
polygons of `scc_08_cs2.tif` give a label with ids 6 (Inflamm/Necrosis, 1,534,829 px) and 11 (SCC, 1,262,015 px).

- **Frame.** 96.3% of the pixels inside the polygons are tissue (darker than 215 in some channel; tissue is
  11.5% of the image) and 96.0% of all tissue pixels are inside a polygon. Moved 150 px down, up, right or
  left, 78, 78, 74 and 74% are tissue; with x and y exchanged, 22%. A cross-correlation over every shift at
  1/8 scale peaks at 0 px down and 8 px right, one search step from zero.
- **Overlaps**, over the 44 `cs2` images: 4.2% of the annotated pixels are under polygons of two classes. Of
  the 356 pairs of different-class polygons that share at least 50 px, 346 are nested (at least 90% of the
  smaller inside the larger) and 10 share a border strip (806 px in total). In all 346 the inner polygon is the
  later in the file, so on this data smaller-on-top equals later-on-top.
- **Fill.** The scanline fill equals a brute-force even-odd test of every pixel centre on 300 random polygons
  (fractional and integer vertices, open and closed outlines, whole grid and a window), and region reads
  (windows, single rows, reversed and stepped slices) equal the same selection of the whole plane.
