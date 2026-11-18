# via-json-polygons

Reads the region annotations of the [VGG Image Annotator](https://www.robots.ox.ac.uk/~vgg/software/via/) (VIA 1
and 2) as an instance label array: every polygon region filled with its 1-based number in its image's entry, on
the grid of the JPEG the entry names, one JPEG per z plane.

## What it reads

- **urls**: the JSON files, as a glob over zip members (`<zip>::<glob>`) or an HTTP directory (`<dir>/::<glob>`),
  or listed one by one. A file is
  - a mapping of entries (VIA's "export annotations as JSON"; the keys are not used),
  - a list of entries, or
  - a VIA project, with the mapping under `_via_img_metadata`.

  An entry is `{"filename": "<image>", "regions": [...]}`, with `regions` a list (VIA 2) or a mapping of
  numbered regions (VIA 1). A region is read from `shape_attributes`: `{"name": "polygon", "all_points_x":
  [...], "all_points_y": [...]}`. `region_attributes` and `file_attributes` are not read.
- **The images.** A VIA file does not hold image sizes, so the snippet lists the images with `params.images`, a
  glob relative to the JSON files' folder (default `*.jpg`; `../*.jpg` when the JSON sits in a subfolder such as
  `.ipynb_checkpoints/`), and reads each plane's height and width from its JPEG's header (the first 64 KiB of a
  stored member, the whole file when it is deflated). No pixel is decoded.
- **The array**: `zyx` (`yx` when there is one image, as `image-tiles-glob` does), uint16, `access: plane`, no
  voxel size. Plane k is the k-th image of the listing, sorted by name, which is the order a raw bound to the
  same glob through `image-tiles-glob` has. Every image must have the first one's height and width.

## Rasterisation

Coordinates are pixels of the image, x to the right and y down, with pixel (y, x) covering `[x, x+1) × [y, y+1)`.
A pixel belongs to a polygon when its centre `(x + 0.5, y + 0.5)` is inside the closed outline by the even-odd
rule (a self-crossing outline leaves its doubly enclosed parts out). Where polygons of one image overlap, the one
covering fewer pixels is on top. 0 is background. A polygon that encloses no pixel centre leaves no id.

## Choosing params

- `images`: where the JPEGs are, seen from the JSON files. All JSON files of one array must lead to one glob.
- `missing`: read the error of the default first; it counts the images without an entry. Use `skip` when those
  images were not annotated (bind the raw to the annotated files only, listed in name order, and set `count`),
  `background` only when an image without an entry is known to show nothing.
- `duplicates`: several exports of one annotation project often repeat entries. Equal copies merge silently.
  When they differ the default error names the image; decide with `first` / `last` which export counts, or bind
  fewer files.
- `count`: the number of planes, so a changed zip is noticed instead of shifting z.

## Known limits

- Polygon regions only. A `rect`, `circle`, `ellipse`, `polyline` or `point` region raises.
- JPEG images only (the grid comes from a JPEG frame header). An image with an Exif orientation other than 1
  and with regions raises: the file does not say whether the coordinates mean the stored or the turned picture.
- A vertex outside the image raises instead of being clipped, since it usually means the wrong image folder.
- Instance ids only; region attributes (class names) are ignored, so a file with several classes is not split.
- An entry naming a file the glob does not match raises. Beside a JSON that is not a zip member, `images` is a
  glob without folder steps (the JSON's own directory index).
- Every plane read costs one range request for its JPEG's header, on top of the JSON files read at open.
