# jpeg-zip-canvas-glob

Reads a set of JPEG images that **differ in height and width**, members of a zip, as one array: one image per
z plane, a colour JPEG's samples as the c axis. The other JPEG readers (`jpeg-zip-glob`, `image-tiles-glob`,
`image-targz-glob`) require every file of an array to have the first file's shape; this one is for sets where
nearly every file has its own, such as object crops cut to the size of each object. It is `jpeg-zip-glob` with
the canvas of `tiff-tiles-canvas-glob`.

**This snippet adds pixels the files do not hold.** An array has one shape, so every plane is a canvas of
`params.canvas` (height, width). An image's pixels sit unchanged in the centre of its plane; the rest of the
plane is 0. That is the only way a set of differently sized images fits the `Array` contract without one
binding entry per file, and it has a price: see *What the fill means*.

- **URLs.** Each URL names one zip that holds images: a plain `<zip url>`, or `<outer zip url>::<inner zip
  member>` for a zip nested in another. An outer member glob (`<outer zip url>::*/crops.zip`) is expanded with
  `io.expand` and stands for every inner zip it matches. Several URLs are concatenated in binding order, and a
  zip named twice raises.
- **`glob`** (required) picks the images inside each zip: an `fnmatch` pattern over member names (`*` also
  crosses `/`), matches sorted by name within a zip (as strings: `Snap_10_...` before `Snap_9_...`). A zip with
  no match raises. The pattern is a param and not a `::<glob>` part of the URL so that the URL is the zip: the
  harness's own `fetch` and `stage` fetch every binding URL, and one zip is one download in a few large
  requests, where a member glob would be one request per image (2,098 for the example, on a host that allows
  133 requests a minute per address). A URL with two `::` raises with that explanation. Anchor the pattern at the image
  folder (`crops/Copepods/*.jpg`): zips made on macOS carry `__MACOSX/.../._name.jpg` sidecars that a bare
  `*.jpg` matches, and those are not JPEGs (the snippet raises `... is not a JPEG` when it reads one).
- **`canvas: [height, width]`** (required). The shape of every plane. An image of `h x w` occupies rows
  `(height - h) // 2` to `+ h` and columns `(width - w) // 2` to `+ w`; every other pixel is 0. An image taller
  or wider than the canvas raises, naming the file and its size: nothing is ever cropped. The first image is
  checked at open, the others when `read` reaches them.
- **Files.** Baseline or progressive JPEG, grey `(Y, X)` or three-sample `(Y, X, 3)` as
  `imagecodecs.jpeg_decode` returns it (YCbCr is converted to RGB by libjpeg), any height and width up to the
  canvas. All images of one array have the first image's dtype and are all grey or all colour; one that differs
  raises at `read`. Four-sample (CMYK) JPEGs are refused.
- **Axes.** `zyx`, or `zcyx` when more than one sample is exposed; `z` is there for one image too. The z axis is
  a stack of independent images in name order, not depth.
- **`samples`** (optional) names the samples of a colour JPEG to expose, as distinct letters of `rgb` in c
  order: `rg` gives c 0 = red, c 1 = green and drops blue; one letter gives a single plane and no c axis.
  Without it all three samples are c. A grey JPEG takes no `samples`.
- **Access.** `plane`: an image is decoded whole whatever the region. The last plane built is kept, so the c
  planes of one image cost one decode. A zip that `io.open` can read by range (a plain URL, a stored member of
  an outer zip) is read that way: its directory at open, each image's bytes when `read` asks (in `io`'s 1 MiB
  blocks). A deflated inner zip, or a server ignoring Range, raises `NeedsFetch` and the whole zip is staged
  with `io.fetch` at open. `open_array` reads every bound zip's directory and decodes the first image (dtype,
  sample count).
- **dtype.** What the decoder returns: `uint8` (`uint16` for a 12-bit JPEG). **Voxel size:** `None`; a JPEG's
  JFIF density is a print size.

## What the fill means

- The 0 around an image is not data. An image may hold real 0 pixels too, so the fill cannot be told from data
  by its value; the image's extent can be recomputed from its height and width only. In the example the crops
  are already masked to 0 outside the organism's outline (41% of their own pixels are exactly 0), so the fill
  continues the files' own background; in a set with a bright or textured background the fill is a black frame
  the images do not have.
- The harness draws crops anywhere on the canvas. A crop therefore shows an image, or a part of it, with black
  around it, and its intensity stretch includes the fill. A crop that holds no image pixel is constant and the
  harness's content filter drops it; so is a crop whose image covers less than 5% of it.
- Keep the canvas tight: the largest height and the largest width among the array's images, no more. One large
  image makes every other plane of the array mostly fill, and fewer of the crops drawn pass the content filter.
  The glob is the only way to split a set, so images of very different sizes can be bound as several arrays
  only where the member names tell them apart.
- The planes are independent images. z is the order of the member names, not depth, and position on the canvas
  says nothing about where a crop lay in the photograph it was cut from.

## Choosing params

- `glob`: one folder per array where the folder means something (a class, a split), anchored at the folder.
- `canvas`: measure it. The sizes are in the JPEG headers only, and the snippet does not look, because that
  means reading every image at `open`. Decode every matching member once (a zip of small crops is one
  download), or `harness describe '<zip url>::<member>'` per file. A canvas that is too small cannot give a
  wrong plane, only an error naming the first image that does not fit.
- `samples`: only for colour JPEGs whose record says some samples are empty or unwanted.

## Known limits

- The fill, above. A set whose images share one shape belongs to `jpeg-zip-glob`, which adds nothing.
- JPEG is lossy: values carry compression noise, also where a background was masked to 0 before the file was
  written.
- The canvas, dtype and grey/colour agreement are checked at `read`, image by image, not at `open` (except for
  the first image).
- Each image is decoded whole and each plane is a full canvas in memory (`height x width` bytes per sample for
  `uint8`); reading a z range stacks that many canvases.
- Read by range, an image costs the 1 MiB blocks it lies in, so a small crop costs up to 2 MiB of the job's
  byte cap; reading every plane costs the zip's size.
- A deflated inner zip is fetched whole at open even when one image is wanted, since its directory is at its
  end.
- No labels: this snippet reads images only. A class given by the folder name (the example) has no
  representation in the binding format; one array per folder keeps the classes apart.
- Colour images, `samples`, nested zips and a server without Range were exercised on synthetic data only; the
  example is grey images in a plain zip URL.

## Example

`examples/zenodo-7979996-zooid-zooplankton.yaml`: Zenodo 7979996 (ZooID), `manuallyVerified_ExtractedImages.zip`
(514.5 MB), 2,098 grey `uint8` JPEG crops of single zooplankton specimens in five class folders, each crop cut
to its organism (69x59 to 2090x3670 pixels; 95 are under 256 pixels high or wide) and masked to 0 outside the
organism's outline. Bound as five raws, one per folder: Amphipods `(270, 2147, 3137)`, Copepods
`(458, 2159, 3797)`, FishLarvae `(621, 2159, 3690)`, Ostracods `(391, 2159, 1767)` and Quetognaths
`(358, 2159, 3839)`, each canvas the largest height and the largest width measured over the folder. All 2,098
members were read through the snippet and compared with a separate reading of the downloaded zip: every plane
holds its file's pixels at the stated place and 0 elsewhere, and every member is in exactly one array. 62% to
89% of an array is fill. The example binding's `snippets_commit` is the commit before this snippet's own (a
binding can't name the commit that adds it); a binding written by the worker names a commit that contains the
snippet.
