# coco-json-polygons

Reads the polygon segmentations of [COCO](https://cocodataset.org/#format-data) JSON files as a label array: the
annotations of each image filled onto that image's grid, one image per z plane. Values are instance ids or COCO
category ids.

## What it reads

- **urls**: the annotated **images**, not the JSON: plain urls, zip members `<zip>::<member>`, or globs
  (`<zip>::<glob>`, `<dir>/::<glob>`), concatenated in binding order; a file named twice raises. Give the label
  array the urls of the raw array it labels. Plane k of both is then the k-th image, whatever the order, and a
  host without a directory index (a Hugging Face `resolve/` tree) needs no listing: the binding lists the files.
  The images themselves are never requested; a url only says which image a plane is and where its COCO file is.
- **The COCO file** of an image is `params.annotations`, a path relative to the image's folder (default
  `_annotations.coco.json` beside the image, as Roboflow exports one per split folder). Images of several
  folders use their own folder's file; each file is read once, whole, when the array is opened. It needs
  - `categories`: `{id, name}`,
  - `images`: `{id, file_name, height, width}`. An image is found by its name, the last path step of its url,
    compared as written with the last path step of `file_name`; exactly one entry must have it,
  - `annotations`: `{image_id, category_id, segmentation}`, `segmentation` a list of rings
    `[x0, y0, x1, y1, ...]` in pixels of the image. `bbox`, `area` and `iscrowd` are not read.
- **The array**: `zyx` (`yx` when the urls are one image, as `image-tiles-glob` does), uint16, `access: plane`, no
  voxel size. The grid is the `height` and `width` the COCO file states for the image; every image of one array
  must state the first one's. The harness checks that grid against the raw's.
- **An image the file lists but no annotation names** is an all-zero plane: COCO lists every image of a split,
  so no annotation means no object was drawn (with `categories`, none of those categories). An image the file
  does not list raises.

## Values

- `ids: instance` (default): an annotation's 1-based number among its image's annotations of the read
  categories, in file order. Ids restart in every plane.
- `ids: semantic`: the annotation's `category_id` as the file states it. It must be 1..65535 (0 is background),
  and all COCO files of the array must give the read categories the same ids and names.
- `categories: [name, ...]` keeps the annotations of those categories only. To get one instance label per class
  (`manual_gt-bacteria-instance`, `manual_gt-flagella-instance`), bind the same urls once per category.

## Rasterisation

Coordinates are pixels of the image, x to the right and y down, pixel (y, x) covering `[x, x+1) × [y, y+1)`. A
pixel belongs to an annotation when its centre `(x + 0.5, y + 0.5)` is inside one of the annotation's rings, each
ring closed and filled by the even-odd rule; an annotation of several rings is their union. Where annotations of
one plane overlap, the one covering fewer pixels is on top (file order breaks ties). The part of a polygon outside
the image is cut off. An annotation that encloses no pixel centre leaves no pixel, so an instance id can be absent.

## Choosing params

- `annotations`: only when the COCO file is not `_annotations.coco.json` beside the images.
- `categories`: look at the file's `categories` (`harness describe <json url>` shows the start of the text).
  Roboflow adds a category 0 named after the project as supercategory; it has no annotations and needs no
  mention.
- `ids`: `semantic` only for a class map in one array; the binding's `segmentation_type` must say the same.

## Known limits

- Polygon segmentations only. A run-length mask (`segmentation` an object, COCO's form for `iscrowd: 1`) raises,
  and so does an annotation without segmentation (a box-only detection file) or a ring of fewer than 3 points.
  Only annotations of the bound images and read categories are checked.
- The grid is the JSON's word. The snippet does not open the image, so a file whose stored pixels are turned
  against the annotated picture (an Exif orientation) is not noticed here; a raw of another shape fails the
  harness's shape check.
- An annotation wholly outside the stated image raises (wrong image or file); one partly outside is cut.
- File names are compared as written: a url whose last step is percent-encoded or carries a query string does
  not match a plain `file_name`.
- Every plane read draws the whole image (about 0.1 to 1 s for a 4096 x 4096 plane with up to 20 polygons); the
  last plane is kept, so the patches of one plane cost one drawing.
- YOLO segmentation `.txt` labels are not read.

## Example

`examples/hf-lynnmass-tilebac-flagella-bacteria.yaml`: the 278 un-resized 4096 x 4096 Roboflow JPEGs of the
TileBac flagella dataset on Hugging Face (`Annotated/COCO/4096/{train,valid,test}`), raw through
`image-tiles-glob` and two instance labels, bacteria and flagella, from the three `_annotations.coco.json`
files. The three COCO files are served by `huggingface.co` itself (1.2 MB together); the JPEGs are Xet-stored,
so `huggingface.co` redirects each to `us.aws.cdn.hf.co`, a host the raw's job must be granted. The example
binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
