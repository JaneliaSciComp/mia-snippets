# png-json-boxes-numbers

Reads a set of numbered 2D images as one array, one image per z plane in **ascending file number** (`z` is kept
for a single image too). The array is either

- **images**: 8- or 16-bit PNG files, grey or colour, with or without alpha (`imagecodecs`). The samples are
  kept as `c` (`zcyx`; a grey file is `zyx`), or only the ones `samples` names; or
- **boxes**: the one COCO-style JSON that annotates those images, rasterised (`zyx`, `uint16`): plane k is the
  image with the k-th file number among `images[]`, on the grid its `width`/`height` give, with the
  `annotations[]` boxes of the chosen categories filled. An image without annotations is an all-background
  plane.

Which of the two is decided by the first file's leading bytes (PNG signature or `{`), never by its name.

It differs from `image-tiles-glob` (PNG tiles, no sample choice, no choice by number, no voxel size) and from
`jpeg-xml-boxes-numbers` (JPEG with one XML per image) in what the annotation is: one JSON for all images, so
the images of an array are chosen by number inside it.

- **URLs.** Images: plain files, zip members `<zip url>::<member>` or globs (`<zip url>::<pattern>`,
  `<dir url>/::<pattern>`, expanded with `io.expand`). Boxes: the JSON, as the array's only url.
- **File numbers and order.** A file's number is the last run of digits in its name, before the extension. The
  name is the last part of the path that ends in `.png`, else the last part, so
  `…/files/350.png/content` and `350.png?download=1` are both 350; in the JSON it is `images[].file_name`.
  Planes are in ascending number in both kinds, whatever the order of the urls or of `images[]`, and two files
  with one number raise. An image array and a box array holding the same numbers therefore pair plane for
  plane. The snippet cannot check that across arrays; the binding's `reasons` say it.
- **One array per shape: `numbers`, `count`.** `numbers: [1, 13, "60-61"]` keeps those images (numbers and
  inclusive runs); every number must exist, so a file that went missing raises instead of shifting z. Without
  it an image array holds all its urls and a box array every image the JSON lists. `count` is the number of
  images the array must end up with. A box array checks at open that all its images have one `width`/`height`
  and names the first that differs. An image array reads the first file's header at open and checks every
  file it reads against it **before decoding**: size, bit depth, samples and pHYs must be the same, since the
  array states one shape and one voxel size.
- **`samples`** (images): the 0-based samples kept as `c`, in that order: `[0, 1, 2]` is the RGB of an RGBA
  file. A file with more than one sample always has a `c` axis, also when one sample is kept.
- **Boxes** (`bbox`, `boxes`, `classes`: required for the JSON, refused for images):
  - `bbox: corners` reads `annotations[].bbox` as `[x1, y1, x2, y2]`, `bbox: xywh` as COCO's
    `[x, y, width, height]`. The file does not say which it is; the binding must.
  - A box covers the pixels `[y1, y2) x [x1, x2)`: 0-based, far corner exclusive. A fractional corner is moved
    outwards (near corner floored, far corner ceiled): every pixel the box touches. A box is cut at the image's
    edge; one with no pixel inside the image raises.
  - `classes`: the `categories[].name` strings whose boxes are painted. Boxes of the file's other categories
    are left out; a name the file does not list raises.
  - `boxes: semantic` fills each box with its category id (ids must be 1..65535). `boxes: instance` fills it
    with its 1-based number among the painted boxes of its image, in `annotations[]` order; ids restart in
    every plane. Background is 0.
  - Boxes are painted largest first, so the smaller of two overlapping boxes is on top, and of two of one size
    the later in `annotations[]`.
  - Everything is checked at open (structure, distinct ids, every annotation's image and category, every
    painted box); `read` only paints.
- **The labels are boxes, not masks**: where a box was drawn, not the object's outline. Whether a binding may
  call that `instance` or `semantic` is a human's decision (see the example binding's `reasons`).
- **Voxel size.** Images: from the first file's `pHYs` chunk when its unit is the metre (`1e9 / pixels per
  metre`, per axis), else `None`. Boxes: `None` (the JSON has none).
- **Access.** `plane`: a PNG is one deflate stream, so any region of an image costs its whole file, read
  through `io.open` (or staged with `io.fetch` on `NeedsFetch`). `open_array` reads the first image's leading
  chunks (one block), or the whole JSON; a box plane costs no further bytes. The last decoded image and the
  last painted plane are kept.
- **dtype.** The PNG's (`uint8` or `uint16`); `uint16` for boxes.

## Known limits

- The shape groups are not discovered: someone has to read every PNG header (or the JSON's `width`/`height`)
  once and write the url lists and `numbers`.
- An image is read and decoded whole into memory (a 7215x5412 RGBA file is 156 MB decoded); no tiles, no
  pyramid.
- A palette PNG or a 1/2/4-bit PNG raises (the decoder would replace the stored values), and so does a file
  whose `tRNS` chunk makes the decoder add an alpha sample.
- Only `bbox` is rasterised: `segmentation`, `iscrowd`, `area` and `score` are not read. More than 65,535
  painted boxes in one image raise in instance mode.
- The JSON is parsed whole at every open of a box array (1.7 MB in the example; `io` keeps the bytes).
- Tested on plain HTTPS urls only; zip members and globs go through the same `io` calls but were not exercised.

## Example

`examples/zenodo-6547151-midog-2022-training-mitoses.yaml`: the MIDOG 2022 training set, 405 H&E regions of
interest as RGBA PNGs (46-86 MB each) on Zenodo and one JSON with 20,552 boxes of 50x50 px (9,501 mitotic
figures, 11,051 hard negatives) in corner format. The PNGs come in many sizes and several pHYs values, so the
binding has one raw per size and pHYs with more than one file, as explicit urls with `samples: [0, 1, 2]`, and
two labels for each annotated raw (`boxes: instance` of the mitotic figures, `boxes: semantic` of both
classes) chosen by the same file numbers. Images 355-405 have no annotations; their groups are bound as raws without
labels. The binding's `reasons` list the groups, what was checked while testing and what a human should
decide. Its `snippets_commit` is the commit before this snippet's own (a binding can't name the commit that
adds it); a binding written by the worker names a commit that contains the snippet.
