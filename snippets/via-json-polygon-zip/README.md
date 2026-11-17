# via-json-polygon-zip

Reads a [VGG Image Annotator](https://www.robots.ox.ac.uk/~vgg/software/via/) (VIA) JSON file and the JPEG
images it annotates, all members of one zip: either the images (`read: image`, a raw: `[z][c]yx` `uint8`, RGB
samples kept as c) or the JSON's polygon regions filled onto the image grid (`read: instance` or `semantic`, a
label: `[z]yx` `uint16`). One z plane per image; a single image has no z. No catalog snippet read VIA JSON or
any polygon JSON before this one.

- **URLs.** One VIA JSON (the url ending in `.json`) and its images, as member urls or member globs
  (`<zip url>::<folder>/annotation.json`, `<zip url>::<folder>/*.jpg`). One array reads one JSON; a second
  JSON is a second array.
- **Planes.** The image urls after glob expansion, sorted; with `annotated_only: true` only the images whose
  VIA entry has at least one region. Bind a raw and its labels with the same urls and the same
  `annotated_only`: the planes are then the same files in the same order, whichever `read` is asked for.
- **Matching.** A VIA entry names its image by file name, so every image url must have an entry with its file
  name and no two image urls may share one; otherwise `open_array` raises and names the files. Entries without
  an image url are not read. Both VIA layouts are read: an annotation export (`{<filename><size>: entry}`) and
  a project file (the same mapping under `_via_img_metadata`); `regions` may be a list (VIA 2) or a mapping
  (VIA 1).
- **An entry without regions is an all-zero plane, which may be wrong.** VIA writes an entry for every image
  that was loaded, annotated or not, so `regions: []` does not say "nothing here". In the example dataset such
  images are full of unannotated animals. Find out what it means, and leave them out with `annotated_only`
  as the example binding does.
- **Grid.** `(Y, X)` and the sample count from the JPEG frame header of the first plane's image (VIA stores no
  image size); nothing is decoded at open. `read: image` checks every image it decodes against that header
  and raises on a difference. 8-bit grey and three-sample JPEGs only.
- **Fill.** Vertices are VIA image coordinates: x to the right and y down from the image's top-left corner, so
  x runs 0..width and pixel (row i, column j) covers x in [j, j+1), y in [i, i+1). A pixel is filled when its
  centre (j + 0.5, i + 0.5) lies inside the closed polygon, by the even-odd rule. A square with corners 0 and 10
  fills 10 x 10 pixels. The part of a polygon outside the grid is cut off (VIA lets a vertex be dragged a little
  past the edge).
  - `read: instance`: each polygon gets its 1-based region number in its image. Ids restart in every plane.
  - `read: semantic`: each polygon gets the 1-based position in `classes` of its `attribute` value.
  - Background is 0. Polygons are painted largest (by filled pixels) first, so where two overlap the smaller is
    on top and file order breaks ties. An overlapped pixel carries one value; a polygon that is wholly covered
    by smaller ones leaves no pixel, so its instance id is absent.
- **`attribute`, `classes`.** Labels only, set together; required for `semantic`, optional for `instance`.
  When set, a region whose attribute is missing or not in `classes` raises at `read`, in both label forms, so
  a class the record does not know is found rather than dropped.
- **Access and requests.** `plane`. Every file is fetched whole with `io.fetch`, once per job (and not at all
  when the dataset cache has it): `open_array` fetches the JSON and the first plane's image, an image `read`
  fetches and decodes the images it is asked for, and a label `read` fetches nothing. A member costs two
  requests (its 30-byte local header and its data), so opening a raw and two labels of one JSON costs four
  beyond the zip's HEAD and directory.
- **429 Too Many Requests.** Each `io` call is repeated when the server answers 429, after the wait its
  `Retry-After` header asks for (1 to 60 s; 60 s when it names none), up to 8 times in all; every other error
  is raised at once. The harness retries dropped connections and 5xx but not 429, and Zenodo's quota (133
  requests a minute) is shared by every job behind one address: in testing, a job of 14 requests met a 429 on
  its fourth. This belongs in the harness's `io`; when it retries 429 itself, the wait here never runs.
- **Voxel size:** `None`; a VIA file holds pixel coordinates only and a JPEG's JFIF density is a print size.

## Known limits

- Polygons only. A `rect`, `circle`, `ellipse`, `polyline` or `point` region raises at `read` and names itself.
- Region and class checks happen at `read`, plane by plane, not at open.
- JPEG images only. An entry's `size` (the image's file size in bytes) is not compared with the member's.
- More than 65,535 regions in one image raise.
- Tested on zip members only; the JSON and the images need not share a folder, but file names must be unique.

## Example

`examples/zenodo-5734143-haemonchus-larvae-maskrcnn.yaml`: one 22.7 GB zip at Zenodo (all members stored) with
135 train and 46 val JPEGs (2160x2560 RGB) of *Haemonchus contortus* eggs and larvae and one VIA JSON per
split, region attribute `Hco_stage` = egg, L2 or L3. Only 69 train and 26 val images carry polygons (6,797 and
3,586); the other 86 have `regions: []` although they show animals, so `annotated_only` leaves them out. Each
split is one raw (`read: image`) with two labels (`instance`, `semantic`): 6 arrays, opened with 10 requests.
The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
