# jpeg-zip-glob

Reads a set of JPEG images that are members of a zip, decoded with `imagecodecs`, and presents it as one
array: one image per z plane, a colour JPEG's samples as the c axis. The zip may be a plain URL or a member of
another zip, which is the case this snippet was written for: a dataset published as one zip whose images sit in
zips inside it.

- **URLs.** Each URL names one zip that holds images: a plain `<zip url>`, or `<outer zip url>::<inner zip
  member>` for a zip nested in another. An outer member glob (`<outer zip url>::*/obj.zip`) is expanded with
  `io.expand` and stands for every inner zip it matches. Several URLs are concatenated in binding order.
- **`glob`** (required) picks the images inside each zip: an `fnmatch` pattern over member names (`*` also
  crosses `/`), matches sorted by name within a zip. A zip with no match raises. The pattern is a param and
  not a third `::` part of the URL because the harness itself resolves binding URLs (`harness fetch` and
  `harness stage` call `io.expand` and `io.fetch` on each one) and understands one `::` level; a URL of the
  form `<zip>::<inner zip>::<glob>` raises here with that explanation. Anchor the pattern at the image folder
  (`val/*.jpg`): zips made on macOS carry `__MACOSX/val/._name.jpg` sidecars that a bare `*.jpg` matches, and
  those are not JPEGs (the snippet raises `... is not a JPEG` when it reads one).
- **Files.** Baseline or progressive JPEG, grey `(Y, X)` or three-sample `(Y, X, 3)` as `imagecodecs.jpeg_decode`
  returns it (YCbCr is converted to RGB by libjpeg). All images of one array must have the same shape and
  dtype; one that differs from the first raises at `read`. Four-sample (CMYK) JPEGs are refused.
- **Axes.** `z` when there is more than one image, `c` when more than one sample is exposed, then `yx`:
  `zcyx`, `zyx`, `cyx` or `yx`. The z axis is a stack of independent images in name order, not depth.
- **`samples`** (optional) names the samples of a colour JPEG to expose, as distinct letters of `rgb` in c
  order: `rg` gives c 0 = red, c 1 = green and drops blue; one letter gives a single plane and no c axis.
  Without it all three samples are c. Use it when the record says channels were saved into some of the colour
  planes and the rest is empty: the harness draws crops from every c plane, an empty one included. A grey JPEG
  takes no `samples`.
- **Access.** `plane`: an image is decoded whole whatever the region. The last decoded image is kept, so the c
  planes of one image cost one decode. A zip that `io.open` can read by range (a plain URL, a stored member)
  is read that way: its directory at open, each image's bytes when `read` asks. A deflated inner zip, or a
  server ignoring Range, raises `NeedsFetch` and the whole zip is staged with `io.fetch` at open.
  `open_array` reads every bound zip's directory and decodes the first image (shape, dtype, sample count).
- **dtype.** What the decoder returns: `uint8` (`uint16` for a 12-bit JPEG). **Voxel size:** `None`; a JPEG's
  JFIF density is a print size.

## Known limits

- JPEG is lossy: values carry compression noise, and with chroma subsampling a strong red or green signal
  leaks a little into the other samples (the example's "empty" blue plane reaches 64 of 255 in places).
- A deflated inner zip is fetched whole at open even when one image is wanted (67 MB for the example's
  largest), since its directory is at its end.
- Image shapes are checked against the first image at `read`, not at open.
- No labels: this snippet reads images only. The example dataset's YOLO box tables (`.txt` beside each jpg) are
  not read by any snippet; the binding format has no box representation yet.

## Example

`examples/4tu-60cdce27-liposome-yolo-boxes-confocal.yaml`: one 972 MB zip at data.4tu.nl holding four deflated
inner zips (`obj.zip` and `val.zip` for a 1-class and a 2-class YOLO set) with 560, 140, 32 and 8 confocal
images of liposomes, 512x512 RGB `uint8` JPEGs with the two channels in red and green. Bound as four raws,
`samples: rg`, each `zcyx` `(N, 2, 512, 512)`. Which of red and green is which stain is not stated in the
record. The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name
the commit that adds it); a binding written by the worker names a commit that contains the snippet.
