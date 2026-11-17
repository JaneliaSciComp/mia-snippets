# tiff-tiles-json-boxes

Reads one large single-page 2D TIFF tile by tile, or the boxes of one COCO-like JSON annotation file filled onto
that TIFF's pixel grid. Written for whole-slide sections with one box-annotation JSON each, where every section
has its own shape and is therefore its own raw/label pair.

Which form an array takes is decided by its urls, and which url is which by the files' first bytes (TIFF magic
or `{`), never by their names:

- **Image form: one url, the TIFF.** `yx` for a grey file, `cyx` for an RGB one (the samples kept as c = R, G,
  B; a YCbCr JPEG file is converted to RGB by the decoder, as `tifffile` does). `read` decodes only the tiles
  (or strips) the region touches, so a 256x256 crop of a 6000x17000 section costs one to four 256x256 tiles
  instead of the 306 MB page that `tiff-zip-member-2` decodes for every read (several GB for the larger sections). This is what it adds for the raw.
- **Label form: two urls, the JSON and the TIFF it annotates** (either order). `yx`, `uint16`, on the TIFF's
  grid. The JSON says where the boxes are but not how large the image is (its `image` key holds a file path
  only), so the shape comes from the TIFF's header; no TIFF pixel is read for a label. No catalog snippet read
  JSON boxes before this one (`tiff-jpeg-voc-zip-glob` and `jpeg-xml-boxes-numbers` read VOC XML, which states
  its own image size).

## The annotation file

```json
{"annotations": [{"bbox": [6851.0, 4972.0, 150, 150], "category": 0, ...}, ...],
 "categories": [{"name": "PMF", "id": 0}, {"name": "Primaire", "id": 1}, {"name": "Secondaire", "id": 2}],
 "image": {"filepath": ".../1C_e/1C_e__roi7.tif"}, "info": {...}}
```

- `bbox` is `[x, y, width, height]` in pixels of the TIFF, the corner at the top left; the box covers the
  pixels `[y, y + height) x [x, x + width)`. The four numbers must be whole (`6851.0` is), else the file raises.
- `category` is an `id` of the file's own `categories`. Other keys (`MarkerGuid`, `info`, `image`) are not read;
  `image.filepath` is not checked against the TIFF url, so the binding's `reasons` must say why the two pair.
- This is not a full COCO file: there is one image per file, no `images` list and no `image_id`, and the
  category key is `category`, not `category_id`. A file without an `annotations` list and a `categories` list
  raises.

## Params (label form only; both required there, both refused for an image)

- `classes`: every category name the file declares, in class id order. A declared category whose name is not
  listed raises, in both modes, so a class the record does not know is found rather than dropped.
- `boxes: instance`: each box is filled with its 1-based position in the file's `annotations` list.
- `boxes: semantic`: each box is filled with the 1-based position of its category's name in `classes` (for the
  example, the file's category id + 1). The ids follow `classes`, not the file's ids.
- Background is 0. Boxes are painted largest first, so where two overlap the smaller one is on top; of two
  equal boxes the later one in the file is on top. An overlapped pixel carries one value; the other box's claim
  to it is lost.
- A box that crosses the image edge is cut to the grid (the example's file has one: fixed-size squares centred
  on follicles near the border). A box with no pixel on the grid, or with a width or height of 0 or less,
  raises: it means the JSON is paired with the wrong image.
- A file with an empty `annotations` list is an all-zero label.

**The labels are boxes, not masks.** The array says where a box was drawn, not where the object's outline is.
Whether a binding may call this `instance` or `semantic` is a human's decision (the record's encoding is
`boxes-table`, for which the binding has no `segmentation_type`); see the example binding's `reasons`.

## Access, dtype, voxel size

- **Image:** `chunk` when the file is read by range (a stored zip member or a Range-capable plain URL):
  `open_array` reads the header and the tile offsets, `read` fetches and decodes the tiles of its region. Up to
  64 MB of decoded tiles are kept, so the three `c` planes of one region decode it once. A deflated member is
  staged whole with `io.fetch` at open and reported as `file`. dtype is the file's own.
- **Label:** `plane`. The JSON (a few kB; fetched whole at open, it is a deflated member in the example) and
  the TIFF header are read at open; `read` paints only the window the region asks for, so a crop is cheap, and
  the whole plane (which the harness reads to aim crops at annotations) is `uint16` at the image's size: 204 MB
  for 6000x17000, proportionally more for a larger section.
- **Voxel size:** `None`. TIFF resolution tags describe print size, and the example's TIFF has none; its
  dataset keeps the pixel size in a separate `offset/<folder>.json`, which this snippet does not read.

## Known limits

- One image per array: no stacking of several files, no glob. Sections of different shapes are separate raws,
  each with its own label entries.
- The TIFF must be one series of one page, `YX` or `YXS` (contiguous samples), not palette: no multi-page
  stacks, pyramid levels, planar (`SYX`) samples or OME/ImageJ hyperstacks (`tiff-zip-member-2` reads those).
- A stripped TIFF written as one strip is decoded whole for any region.
- More than 65,535 annotations in one file raise.
- Tested on real data with JPEG (YCbCr) tiles from a deflated zip member (staged whole); reading by byte range
  (a stored member, a plain URL), other codecs and strips are covered only by synthetic files (JPEG, LZW, zlib
  and uncompressed; tiles and strips; grey `uint16` and RGB `uint8`).

## Example

`examples/zenodo-12804564-mouse-ovary-follicle-boxes.yaml`: H&E sections of mouse ovaries (Zenodo 12804564,
one 20 GB zip, its TIFF and JSON members deflated) with follicles marked by class-sized squares (150 px PMF, 300 px
Primaire, 500 px Secondaire). The record's sample section `1C_e__roi7` (6000x17000 RGB, 256x256 JPEG tiles) is
bound as a `cyx` raw (staged whole, 3.2 MB, so `file` access; regions are still decoded tile by tile), and its 12 boxes twice: `boxes: instance` and `boxes: semantic`. The other 642 sections
have the same layout by the zip listing and each would be bound the same way, as its own raw and labels; the
dataset's `roi_annotation_united/all_annotations.json` lists 7,941 boxes over the 643 sections (27 sections
without any, 707 overlapping pairs, none hidden completely), in another layout that this snippet does not read.
The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
