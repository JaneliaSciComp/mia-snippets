# png-tint-overlay-mask-glob

Reads a set of **tint overlays** as one binary mask array, one file per z plane. A tint overlay is an 8-bit RGB
PNG that shows a grey image with its annotated pixels tinted in one colour, the way an annotation tool exports
"image + label" for viewing: the mask is not stored as numbers, only as the colour of some pixels.
`image-tiles-glob` opens such a file but returns the rendering itself (three `c` planes of pixel values), and
its `samples: grey` refuses it because the samples differ, which is exactly where the annotation is.

- **The rule.** A pixel of a grey image saved as RGB has three equal samples; a tinted pixel does not. The mask
  is `1` where a pixel's samples are not all equal and `0` elsewhere (`uint8`). The grey values under the tint
  are not used and the image the overlay was drawn on is not read.
- **The check that makes the rule safe.** A tint blended over whole pixels shifts the samples by the same
  `(G - R, B - R)` wherever it is drawn. Where two annotation shapes overlap, the tool blends it a second time,
  which gives a second, larger shift of the same colour (a 25% blue reads B - R = 64 once and 112 twice), and
  so on for each depth of overlap. So in one file the tinted pixels may carry at most 8 different shifts, and
  they must all be one colour: each a positive multiple of the largest, give or take one per sample for
  rounding. A file that breaks either raises at `read` and names two of the shifts: a real colour image, an
  overlay with anti-aliased or semi-transparent edges and a lossy (JPEG-recompressed) overlay have many shifts;
  an overlay with two classes in two colours has two that are not multiples. Every tinted pixel is `1`
  whatever its depth. Each file is checked on its own; the tint may differ from file to file.
- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`), expanded with `io.expand`. Several URLs are concatenated in binding order, and a file named
  twice raises.
- **Order: by the image, not by the overlay.** The overlay of `<stem>.<image_ext>` is `<stem><suffix>.png` in
  the same folder (defaults `_label` and `jpg`). The files one URL stands for are ordered by the name of the
  image each belongs to, which is how `io.expand` orders a glob over those images. Sorting the overlays by
  their own names gives another order: `Grid A8_2.jpg` sorts before `Grid A8_23.jpg` (`.` < `3`), but
  `Grid A8_23_label.png` before `Grid A8_2_label.png` (`3` < `_`). So a raw bound as `folder/*.jpg` with
  `image-tiles-glob` and a label bound as `folder/*_label.png` with this snippet pair plane for plane.
- **The image must be there.** For a zip member, a member glob or a directory glob, the listing the files came
  from (`io.list`, no further bytes) must hold each overlay's image; an overlay without one raises at open and
  names it, as does a file whose name does not end in `<suffix>.<extension>`, or two overlays that name the same
  image. The other direction, an image without an overlay, makes the label array shorter than its raw, which
  smoke reports. A plain URL listed by hand is taken in binding order and not checked.
- **Files.** PNG colour type 2 (RGB) at bit depth 8, interlaced or not. Greyscale, palette, RGBA and 16-bit
  PNGs, a tRNS chunk, and files that are not PNG raise (the header at open for the first file, at `read` for
  the others). Every file of one array must have the first file's height and width.
- **Axes.** `zyx`, or `yx` with one file, as `image-tiles-glob` does for the images. **dtype** `uint8`, values
  0 and 1. **Voxel size:** `None`; a PNG's pHYs chunk describes print size.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads the first file's 33-byte header and
  decodes nothing; a file is decoded when `read` asks for its plane, and the last mask is kept.
- **Params** (both optional): `suffix`, the text between an image's stem and its overlay's extension (default
  `_label`; may be empty for `<stem>.png`); `image_ext`, the image's extension without the dot (default `jpg`).

## Known limits

- One tint colour per file, blended over whole pixels. Anything softer raises rather than being thresholded,
  and so does a file with more than 8 depths of overlap. The limit of 8 is a judgement: the example has at most
  2, and an anti-aliased edge of the same tint would have up to 63. An edge softened into 8 levels or fewer
  would pass, and its faint rim would be read as annotated.
- A pixel is "tinted" only if its samples differ. A tint that is itself grey (white, black) cannot be seen, and
  an overlay with nothing drawn on it is an all-background plane without any error: it cannot be told from a
  grey image saved as RGB. A tint that clips at 255 on bright pixels changes its shift there: such a file
  raises once the clipped shifts are more than 8, and below that the clipped pixels are still read as tinted.
- The image under the overlay must be grey. An overlay drawn on a colour image has differing samples
  everywhere and raises.
- One class. The mask says where the tint is, not which colour it is; `segmentation_type: binary`.
- The mask is the one the overlay shows. If the tool that drew the overlay simplified the annotation (drew
  outlines only, or only the visible layer), that is what is read.
- The image is named, not read: its size is not compared with the overlay's (smoke compares the label array
  with its raw), and the snippet cannot check that the raw is bound in the same order; the binding's `reasons`
  say so.
- Each file is read and decoded whole into memory (2048 x 2048 x 3 here). No pyramid levels.
- On real data only deflated zip members were read; stored members, directory globs, plain URLs and the
  refusals were exercised on a local range server only.

## Example

`examples/zenodo-7025602-nanowire-tem-morphology.yaml`: TEM images of peptide and protein nanowires in four
morphology classes (Zenodo 7025602), one zip per class with 100 JPEGs of 2048 x 2048 (grey, stored as three
identical samples). `dispersed.zip` and `network.zip` also hold one `<name>_label.png` per image: the image
with the segmented nanowires blended 25% blue, so R = G everywhere and B - R is 64 on a nanowire and 0
elsewhere. Raw is read by `image-tiles-glob` with `samples: grey` as `zyx` (100, 2048, 2048) per class; the
label by this snippet as `zyx` (100, 2048, 2048) for the two classes that have overlays. In `dispersed.zip` the
two sorted name lists disagree at 67 of the 100 positions; `network.zip` sorts the same either way.

The dataset also ships the masks as numbers (`seg_mask_5_resolutions.zip`, `seg_mask_res<N>.npz`, 200 masks
resized to at most 1024 x 1024), but in an order no file documents, so they cannot be paired with the images
without guessing; they were used to check this snippet instead. While testing, all 200 planes were read
through the snippet from the real zips:

- R = G in every file. B - R is 0 or 64 in 198 files; `network/Grid B2_383_label.png` and
  `network/Grid B2_459_label.png` also have 112 on 1,749 and 5,526 pixels, where two annotation shapes overlap
  (the grey there is the image times 0.75 twice). The first version of this snippet allowed one shift per file
  and refused those two files; that is why a tint may be blended more than once.
- No plane is empty: 1.3% to 24.5% of a dispersed plane is nanowire (mean 12.5%), 25.3% to 49.8% of a network
  plane (mean 37.8%).
- The overlay's grey outside the mask correlates with the raw plane of the same z index at 0.946 or more
  (0.999 in 73 files; the other 127 overlays were drawn on a contrast-stretched copy of the image and are at
  0.946 to 0.98), so the planes pair as the names say.
- Each mask, reduced 2 x 2, is closest to a different one of the 200 masks of `seg_mask_res1024.npz`: all 200
  are used once. Wherever a 2 x 2 block is all nanowire or all background the two agree, in 199 files without
  exception; intersection over union with the reduced mask is 0.980 to 0.999. The npz order is not the name
  order (dispersed and network are interleaved), which is why the npz is not bound.
- The one difference: in `Grid B2_383`, 514 of those blocks (about 2,000 pixels, 0.05% of the plane) beside
  the overlap are tinted in the overlay and 0 in the npz. The overlay shows one annotation shape there that
  the npz does not have; this snippet reads what the overlay shows.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
