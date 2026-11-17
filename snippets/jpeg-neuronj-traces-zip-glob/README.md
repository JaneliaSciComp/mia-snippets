# jpeg-neuronj-traces-zip-glob

Reads the images of a zip that were traced with NeuronJ, and their tracings on the same grid. NeuronJ's
"export tracings" writes one text file per tracing beside the image: `<image>.N<k>.txt` for `<image>.jpg`, one
`x<TAB>y` pixel coordinate per row along neurite k. No catalog snippet read a coordinate table before this one.

The same urls are bound twice: once for the images (raw) and once with `traces: instance` (label). Both arrays
then hold the same images in the same order, one per z plane (`z` is dropped for a single image).

- **URLs.** Zip members only: `<zip url>::<member>` or a member glob `<zip url>::<pattern>` (`fnmatch` syntax;
  `*` also crosses `/`), expanded with `io.expand` and sorted by name; several URLs are concatenated in binding
  order. The URLs name the **images**, also for the traces. A URL that is not a zip member raises.
- **Only traced images are in the array.** An image is kept when the zip has at least one member
  `<image without extension>.N<k>.txt`; an image without one is left out of both the images and the traces.
  An untraced image is not known to be free of neurites, so it gets no empty label plane. If no image has a
  trace file, `open_array` raises. Bind the untraced images with another snippet if they are wanted.
- **Images** (no `traces`): 8-bit JPEG, checked by its first bytes (a member that is not a JPEG raises and
  names itself, so leave such members out of the glob). Decoded by libjpeg. An RGB image is three `c` planes
  (`zcyx`, c = R, G, B); with `samples: grey` it is one plane (`zyx`) and an image whose samples are not
  identical copies raises. A grey JPEG is `zyx` either way. `uint8`.
- **Traces** (`traces: instance`): `zyx`, `uint16`, 0 background. Each `<image>.N<k>.txt` is drawn with the
  value k of its file name, so ids are NeuronJ's tracing numbers (`N7` is 7, as in the `Tracing` column of
  `<image>_tracings.csv`) and restart in every plane; numbers may have gaps.
  - The rows are the polyline's vertices, in pixel indices of the image (x = column, y = row, from 0). Every
    vertex is set, and between two vertices the pixel nearest to the straight line at each step along the
    longer axis, so a tracing is an 8-connected line one pixel wide. NeuronJ exports its vertices a few
    pixels apart; the straight pieces between them are this snippet's, not the tracer's.
  - Tracings are drawn in ascending k. Where two share a pixel (a branch leaves its parent, two neurites
    cross) the higher k is kept and the other's claim to that pixel is lost.
  - The grid is the image's: height and width come from the JPEG frame header of the image the traces belong
    to, read at `read` for every plane. A vertex outside it raises, as does a row that is not two integers,
    an empty file, a number outside 1..65535 or two files with the same number (`N1`, `N01`).
- **The labels are centre lines, not masks.** A plane says where the tracer's path runs, not how wide the
  neurite is, and only what was traced is labelled: untraced neurites and the cell bodies are background.
- **Shape.** Every image must have the first image's height, width and sample count; one that differs raises
  at `read` and names itself. Bind images of another size as another array.
- **Access.** `plane`. An image is read with `io.open`, or staged whole with `io.fetch` when its member is
  deflated. The trace files of one image are small and lie side by side in the zip, so they are read from the
  zip by range (`io.open` of the zip, 1 MiB blocks) instead of member by member: one or two requests per plane
  rather than two per tracing, which a host with a request limit (Zenodo: 133 per minute) refuses for an image
  with dozens of tracings. A zip that cannot be read by range (`NeedsFetch`) falls back to `io.fetch` per
  member. `open_array` reads the zip's directory and the first traced image (its header only is parsed; no
  pixel is decoded). The last plane made is kept, so several crops of one plane cost one read.
- **Voxel size:** `None`; neither the JPEG header nor a NeuronJ export states a pixel size.

## Params

| param | values | meaning |
|---|---|---|
| `traces` | `instance` | the array is the tracings, drawn as above; without it the array is the images |
| `samples` | `grey` | images only: return sample 0 of RGB images whose samples are identical; refused with `traces` |

## Known limits

- NeuronJ's other files are not read: the tracing type and cluster (`<image>_tracings.csv`, `.ndf`), so there
  is no semantic (axon/dendrite, primary/secondary) labelling, and the `.ndf` itself, which holds the same
  paths. Trace files named otherwise than `<stem>.N<k>.txt` (e.g. without `.txt`) are ignored.
- The pairing is by name only: nothing in a trace file names its image. A trace file whose image is not among
  the URLs is ignored.
- Lines are one pixel wide; no width, dilation or anti-aliasing. JPEG EXIF orientation is not applied (NeuronJ
  traces on the stored pixel grid, as ImageJ shows it).
- Shape and sample checks beyond the first image happen at `read`, not at `open`.
- A label plane fetches its image (for the header) as well as its trace files; a crop job reads that image
  anyway. Reading the traces by range means an offline re-run needs the zip itself in the cache, not only
  its fetched members.
- No 12-bit, CMYK or lossless JPEG, no PNG or TIFF images, no plain (non-zip) URLs or HTTP directories.

## Example

`examples/zenodo-6824724-rat-hippocampal-neurons-neuronj-traces.yaml`: bright-field images of rat hippocampal
neurons (3024 x 3226, grey stored as RGB) with NeuronJ tracings, all in one Zenodo zip.
