# via-json-polygon-zip

Reads the polygon regions of one [VGG Image Annotator](https://www.robots.ox.ac.uk/~vgg/software/via/) (VIA)
JSON file as a label array on the grid of the images it annotates: one z plane per image (`zyx`, or `yx` for a
single image), `uint16`. The JSON and its images are members of the same zip, in the same folder. No catalog
snippet read VIA JSON or any polygon JSON before this one. It reads labels only; the images themselves are read
by an image snippet such as `jpeg-zip-glob`.

- **URL.** One zip member, `<zip url>::<folder>/<name>.json` (a member glob that matches exactly one file is
  fine). One array reads one JSON; a second JSON is a second label.
- **Planes.** The members of the JSON's own folder (not of subfolders) whose file name matches `images`
  (`fnmatch`, default `*.jpg`), sorted by name. That is the order in which `jpeg-zip-glob` stacks
  `<folder>/<the same pattern>`, so plane k of this label is the annotation of plane k of that raw. The snippet
  cannot check this across arrays: the binding's `reasons` must say that the two patterns pick the same files.
- **Matching.** Every picked image must have exactly one VIA entry whose `filename` is its file name, and every
  entry of the JSON must name a file of the folder (picked by `images` or not); otherwise `open_array` raises
  and names the files. Both VIA layouts are read: an annotation export (`{<filename><size>: entry}`) and a
  project file (the same mapping under `_via_img_metadata`); `regions` may be a list (VIA 2) or a mapping (VIA 1).
- **An entry without regions is an all-zero plane, which may be wrong.** VIA writes an entry for every image
  that was loaded, annotated or not, so `regions: []` does not say "nothing here". In the example dataset such
  images are full of unannotated animals. Find out what it means and leave unannotated images out with `images`
  (and the raw's glob), as the example binding does.
- **Grid.** `(Y, X)` from the JPEG header of the first picked image (its first 64 KiB, by range when the member
  is stored; a deflated member is fetched whole). VIA stores no image size. The other images' sizes are not
  read: the paired raw snippet checks that they agree.
- **Fill.** Vertices are VIA image coordinates: x to the right and y down from the image's top-left corner, so
  x runs 0..width and pixel (row i, column j) covers x in [j, j+1), y in [i, i+1). A pixel is filled when its
  centre (j + 0.5, i + 0.5) lies inside the closed polygon, by the even-odd rule. A square with corners 0 and 10
  fills 10 x 10 pixels. The part of a polygon outside the grid is cut off (VIA lets a vertex be dragged a little
  past the edge).
  - `fill: instance`: each polygon gets its 1-based region number in its image. Ids restart in every plane.
  - `fill: semantic`: each polygon gets the 1-based position in `classes` of its `attribute` value.
  - Background is 0. Polygons are painted largest (by filled pixels) first, so where two overlap the smaller is
    on top and file order breaks ties. An overlapped pixel carries one value; a polygon that is wholly covered
    by smaller ones leaves no pixel, so its instance id is absent.
- **`attribute`, `classes`.** Set together; required for `semantic`, optional for `instance`. When set, a region
  whose attribute is missing or not in `classes` raises at `read`, in both fills, so a class the record does not
  know is found rather than dropped.
- **Access.** `plane`: reading any part of a plane fills all of that image's polygons; the last plane is kept.
  `read` fetches nothing: `open_array` has read the zip's directory, the JSON and the first image's header.
- **dtype.** `uint16`. **Voxel size:** `None`; a VIA file holds pixel coordinates only.

## Known limits

- Polygons only. A `rect`, `circle`, `ellipse`, `polyline` or `point` region raises at `read` and names itself.
- Region and class checks happen at `read`, plane by plane, not at open.
- JPEG images only (the grid comes from a JPEG header), and only in the JSON's own folder of the same zip; the
  entry's `size` (the image's file size in bytes) is not compared with the member's.
- More than 65,535 regions in one image raise.

## Example

`examples/zenodo-5734143-haemonchus-larvae-maskrcnn.yaml`: one 22.7 GB zip at Zenodo (all members stored) with
135 train and 46 val JPEGs (2160x2560 RGB) of *Haemonchus contortus* eggs and larvae and one VIA JSON per
split, region attribute `Hco_stage` = egg, L2 or L3. Only 69 train and 26 val images carry polygons (6,797 and
3,586); the other 86 have `regions: []` although they show animals, so they are left out. The annotated images
are bound as 12 raws (`jpeg-zip-glob`, one per file-name pattern) with two labels each, instance and semantic.
The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
