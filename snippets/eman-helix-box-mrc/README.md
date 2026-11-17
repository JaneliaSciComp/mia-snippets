# eman-helix-box-mrc

Reads one **EMAN helix `.box`** text file as a label: the helices it lists, filled as instance ids on the pixel
grid of the **MRC** micrograph or movie stack they were boxed on. It reads labels only; the MRC itself is read
with `mrc-http-3`.

## The file

Text, one box per line, five whitespace-separated columns: `x y w h flag`. `x`, `y` is the corner of a `w` x `h`
box in micrograph pixels. A helix is two consecutive lines, the start box (flag `-1`) and the end box (flag
`-2`):

```
3005	1594	280	280	-1
3856	1637	280	280	-2
```

This is what EMAN1's boxer writes in helix mode and what EMAN2's `e2helixboxer` writes as "EMAN1 box
coordinates": the user marks the two ends of a helix and gives a width; each end is stored as the square box of
that width centred on it (corner = end point - width / 2). So

- the **axis** of the helix runs from the centre of the start box, `(x + w/2, y + h/2)`, to the centre of the
  end box;
- the **width** of the helix is the box size.

## URLs and params

`urls: [<the .box>, <the MRC it was boxed on>]`, in either order; the `.box` is recognised by the ending of its
url path. No params.

A `.box` file does not say how large its micrograph is, so the snippet reads the MRC's 1024-byte header for the
shape (and the voxel size the header states). No MRC pixel is read. Which MRC belongs to a `.box` is the
binding's statement (here: the same stem, `<name>.box` and `<name>_stack.mrc`); the snippet can only check that
every helix axis lies on that grid, and raises when one does not.

## The array

- **`zyx`, `uint16`, the MRC's `(nz, ny, nx)`**, which is the shape `mrc-http-3` gives the same file, so the
  label matches its raw on every axis.
- **Each helix is filled as the rectangle of its width around its axis**: a pixel gets the helix's id when its
  centre `(col + 0.5, row + 0.5)` is at most `width / 2` from the axis and, measured along the axis, between the
  two end points. That is the helix as the boxing programs define it (the region their segments are cut from),
  not the two end boxes: those mark where the helix ends, and painting them would label two squares and leave
  the filament between them as background. The rectangle is not extended past the end points, so the outer
  halves of the two end boxes are not part of it.
- **Ids** are the 1-based number of the helix in its file (first pair 1, second pair 2, ...), 0 is background.
  Where two helices cross, the later one in the file is on top.
- **`z` is the movie frame, and every frame holds the same label.** A `.box` is drawn on the micrograph, not on
  a frame, and the frames of a dose-fractionated stack are exposures of the same field, so the one 2D label is
  repeated over the MRC's `nz`. Beam-induced drift between frames is not in the file and is not modelled; it is
  small against the box width (see Known limits). For a single micrograph (`nz` = 1) the label is `zyx` with one
  plane, as `mrc-http-3` presents the micrograph.
- **Coordinates** are array indices of the MRC as stored: `x` is the column, `y` the row counted from the first
  row in the file, the frame `mrc-http-3` returns (EMAN's own convention; its display puts row 0 at the bottom,
  the numbers do not change). See "Tested on" for the check.
- **Clipping.** An end box may reach past the image edge (a helix that leaves the field of view is boxed up to
  the edge, so corners can be negative); the rectangle is cut at the edge. An end *point* outside the image
  raises.
- **An empty file** is a micrograph with no helix boxed: an all-background label.
- **Voxel size**: what the MRC header's cell says, computed as `mrc-http-3` does, or `None`.
- **Access `plane`.** The two files are read at open: the `.box` (here 40 to 250 bytes) and the first 1024
  bytes of the MRC, for which `io` fetches the file's first block (about 1 MB per label in testing). `read`
  fetches nothing: it paints the requested rows and columns from the parsed table.

## Refused at open

- urls that are not exactly one `.box` and one other file; an MRC with an axis order other than 1 2 3 or a mode
  `mrc-http-3` does not read.
- A line that is not five columns, a flag other than `-1` / `-2` (e.g. a particle box file, whose boxes are not
  helix ends), flags that are not start/end pairs in order, a non-square box, a start and end box of different
  sizes, a helix of zero length, an end point outside the MRC's grid, more than 65,535 helices.

## Known limits

- **The labels are boxes, not masks.** The filled band is as wide as the box the annotator chose, which need
  not be the filament's own width: in the tested dataset 27 of 28 files use 280 px (29.7 nm at 1.061 A/px)
  around a tobacco mosaic virus rod 18 nm (about 170 px) across, one file uses 170 px. A consumer must not take
  the band's edge for the virus outline.
- **Straight helices only.** A helix is one straight segment between two points; a bent filament boxed as
  several pairs becomes several ids.
- **Not every filament is boxed.** 0 means "not boxed", not "no virus": annotators box the straight,
  well-separated stretches they want to reconstruct.
- **Frames are not aligned.** The label ignores inter-frame drift (typically a few pixels over an exposure at
  this pixel size, against bands of 170 to 280 px).
- Only the `x y w h flag` text form with `-1` / `-2` pairs. EMAN2's helix `.txt` / JSON coordinates, RELION
  `.star` and plain particle `.box` files need another snippet.
- Fractional coordinates are accepted (the format writes whole numbers); no such file was met.

## Tested on

`empiar-10021-tmv-falcon2-lmb-helix-boxes` (examples/): 28 Falcon II movie stacks (`float32`, 16 x 4096 x 4096)
of tobacco mosaic virus and one `.box` each, 1 to 6 helices per file, 84 in all, 292 to 3219 px long; 14 end
boxes have a negative corner (helices boxed up to the image edge) and every end point lies on the 4096 x 4096
grid. All 28 labels were opened; the patch read is from the first.

Frame check on frame 0 of `Falcon_2014_03_14-14_39_53_stack.mrc` (2 helices). Tobacco mosaic virus has a strong
23 A layer line, a period of 21.68 px along the rod at the record's 1.061 A/px. The band of helix 2 (2189 px
long) was resampled along its axis and the power at that period compared with the neighbouring frequencies: at
the stated position it stands 24.7 standard deviations above them (peak at 21.66 px), against 3.4 with y
flipped, -0.1 with x flipped, 0.4 with x and y exchanged, and 0.9 to 1.4 with the band moved 300 or 600 px
sideways. In 60 px strips the signal is 31.2, 8.0 and 9.0 within 90 px of the axis and at most 2.3 from 90 to
270 px away: a rod about 170 px across, centred on the line between the two box centres, inside the 280 px
band. So the axis is the line between the box centres, in the row order of the file, with no flip or transpose.
Helix 1 of that file (852 px long) shows no clear layer line in a single frame at any of these positions (1.9
as stated), so it neither confirms nor contradicts. Only this one micrograph, and only one of its 16 frames,
was looked at (the host served about 100 kB/s); the other 27 were opened but not compared with their pixels.

Synthetic tests (not in the PR, which may add one directory only): horizontal, vertical, reversed and diagonal
helices against hand-computed masks, crossing helices, clipping at the image edge, an empty file, strided and
single-row reads, both MRC byte orders, either url order, and every refusal listed above.
