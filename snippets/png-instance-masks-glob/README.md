# png-instance-masks-glob

Reads instance labels stored as one binary mask PNG per object, `<stem><mask><k>.<ext>`, in the same folder as
the image they annotate, `<stem><image>`, and presents them as one array of instance ids: one plane per image,
the images stacked along z. It exists because `image-tiles-glob` gives one z plane per file (1134 planes for
the example's 282 images) and `png-instance-masks-zip` reads zip members named `<image>_<k>.png` only, makes
no plane for an image without masks, and orders the planes by the masks' names, not the images'.

- **URLs.** The urls name two kinds of file, in any order: the masks and the images they belong to. Each url
  is a plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>` or a glob over
  an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*` also crosses `/`), expanded with
  `io.expand`. A file named twice raises. A file is told by its name:
  - a **mask** is `<stem><mask><k>.<ext>`: `params.mask`, then a decimal number `k` from 1 to 65535, then any
    extension (`CN_001_Mit01.png` with `mask: _Mit` is mask 1 of stem `CN_001`);
  - otherwise an **image** is `<stem><image>` (`CN_001_Original.png` with `image: _Original.png`);
  - any other name raises, so the globs have to select these two kinds only.

  `<stem>` is everything before, folder and zip included, so a mask belongs to the image beside it.
- **Planes and their order.** One plane per image, in the order the image urls expand in: binding order
  across urls, sorted by name within a glob. That is the order in which `image-tiles-glob` stacks the same
  urls, so give this snippet the raw's image urls (in the raw's order) and plane z of the label is the image
  at plane z of the raw. The images are **not read** (no byte of them is fetched for the label); only their
  names are used. The masks' urls may come in any order.
- **The images select.** The masks of an image that is not among the urls are left out, so a set of images
  that has to be bound as several arrays (one per image size, say) takes the same mask glob in each, beside
  that array's image urls. Urls with no image, with no mask, or where no mask belongs to any of the images
  raise.
- **An image without a mask file is an all-zero plane.** The snippet cannot tell "no object in this image"
  from "the mask glob missed this image's masks": the binding's `reasons` must say how many images have no
  mask.
- **Ids.** In a plane, a pixel is `k` where mask `k` is non-zero and 0 where no mask is. `k` comes from the
  file name, so ids are the dataset's own numbering and a missing `k` leaves a gap, not a shifted id. Two
  files that are the same `k` of one image (`_Mit1` and `_Mit01`, or `_Mit01.png` and `_Mit01.jpg`) raise. Ids
  are unique within a plane, not across planes.
- **Overlap.** Masks are painted in ascending `k`: where several masks are non-zero the pixel gets the
  **highest** `k`. The lower masks lose those pixels; the snippet does not report it.
- **Mask files.** PNGs, told by the file's first bytes and not by its extension: the example dataset has PNG
  masks named `.jpg`. A real JPEG raises (lossy, so it has no exact zero). A grey PNG of any bit depth is the
  object where its value is not 0. A PNG that decodes with samples (RGB, RGBA, grey + alpha, palette) is read
  when it is a grey mask in disguise, as one file of the example is (RGBA, R = G = B, alpha 255): the colour
  samples of every pixel must be equal and the alpha, if there is one, fully opaque everywhere; then the
  object is where the colour is not 0. Samples that differ raise (no single value to compare with 0) and so
  does any transparent pixel (whether it is background is not the reader's to decide).
- **One size per array.** Every mask must have the size of the first mask of the array's first image that
  has one; one that differs raises at `read`, before it is decoded. Images of several sizes are bound as one
  array per size.
- **Axes** `zyx`, or `yx` when the urls name one image. **dtype** `uint16` always. **Voxel size** `None`.
- **Access.** `plane`: a plane is all the masks of one image, whatever the region asked for. Each mask is read
  through `io.open`, or staged with `io.fetch` when that raises `NeedsFetch` (a deflated zip member).
  `open_array` expands the urls and reads one mask (the first image's lowest `k`) for the plane size; the
  last plane merged is kept.

## Params

Both are required, and both are parts of a file name (no `/`).

- `mask`: the text between the stem and a mask's number, e.g. `_Mit`.
- `image`: the end of an image's file name after the stem, extension included, e.g. `_Original.png`.

A name is tried as a mask first. So with images named `<stem>.png` and masks `<stem>_<k>.png`
(`mask: _`, `image: .png`) an image whose own stem ends in `_<number>` would be taken for a mask of an image
that does not exist and be left out: the label then has a plane fewer than the raw, which the harness reports.

## Known limits

- Masks and images must share a folder and a stem. Masks in a sibling folder (`masks/<stem>_<k>.png` beside
  `images/<stem>.png`) are not matched; `png-instance-masks-zip` reads that layout from a zip.
- The label's grid is the first mask's size. It is not compared with the image files (they are not read);
  the harness compares the label's shape with the raw's.
- Nothing reports which masks the image urls left out; a mask glob that misses masks, or image urls that miss
  images, give fewer objects or planes without an error.
- No param chooses the overlap rule, relabels ids or thresholds a mask: any non-zero value is the object.
- Each mask file is asked for on its own (the example's are 5 kB each, 1 to 14 per plane), and there is no
  waiting on a `429 Too Many Requests` answer as `png-instance-masks-zip` has.
- The snippet fetches no byte of an image, but `harness stage` stages every url of a binding before it opens
  the arrays, the image urls of the label among them. They are the raw's files, so that costs nothing more.
- Zip members were exercised with a stand-in `io` only (stored and `NeedsFetch` members), not on a real zip.

## Example

`examples/s-biad4140-chikv-mitochondria-tem.yaml`: 282 TEM images of CHIKV-infected and mock HEK-293T cells in
one HTTP directory (BioStudies S-BIAD4140), each `{CN,WT}_NNN_Original.png` (8-bit grey) with an
all-mitochondria mask `_FullMit.png` and one mask per mitochondrion `_MitNN.png` (values 0 and 255), 1 to 14 per
image, 1134 in all. The PNG headers of all 1698 image and mask files show what one sample did not:

- 11 images (`CN_030`, `CN_041` to `CN_050`) and their masks are 2098x2098, the other 271 are 2048x2048;
- the three masks of `WT_005` are named `.jpg` and are PNG files;
- `WT_071_FullMit.png` and `WT_071_Mit01.png` (the same file twice) are RGBA, R = G = B, alpha 255.

So the binding has three raws read by `image-tiles-glob` (270 images of 2048x2048, 11 of 2098x2098, `WT_071`
alone), the `_FullMit` label by `image-tiles-glob` for the first two, and this snippet's label for all three:
`zyx` (270, 2048, 2048), `zyx` (11, 2098, 2098) and `yx` (2048, 2048), uint16, each from the one mask glob
`*_Mit[0-9][0-9].*` and its raw's image urls.

Checked on every plane of the three arrays (282 images, 1134 masks; 21 MB with the first raw plane of each
array): the label's image urls are the raw's file urls plane for plane; the ids in a plane are exactly the
numbers of the image's mask files; the plane equals a merge of the same files made apart from the snippet;
and on the 281 planes with a `_FullMit` label the non-zero pixels are exactly that file's. Masks overlap in
12 images, 24,305 pixels in all (15,468 of them in `CN_061`), where the highest number was kept; no id was
lost to it. `harness describe` (the central 1024x1024 of 5 planes per array) finds one 4-connected component
per id on 8 of the 9 planes that hold an id there, and one id in two pieces on the other. A local
`harness crop --n 12` made 12 crops (28 MB) that `harness verify` faults only for the licence, which the
issue's copy of the record does not have.

The naming rules, the all-zero plane of an image without masks, overlap, `yx` for one image, region reads,
zip members (stored and `NeedsFetch`), RGB, RGBA, grey + alpha, palette, 1-bit and 16-bit masks and every
error above were exercised with synthetic files over a stand-in `io`; the example has no image without masks
and no zip.

The smoke run warns `label 2: dtype uint16 is not the record's ['uint8']` (and the same for labels 3 and 4):
the record describes the single mask files, the snippet returns ids. The example binding's `snippets_commit`
is the commit before this snippet's own (a binding can't name the commit that adds it); a binding written by
the worker names a commit that contains the snippet.
