# image-sperm-table-discs

Reads one 2D image, or the per-sperm table that annotates it, from plain URLs. The two forms are chosen by
`draw`:

- **Image form** (no `draw`, one url): one PNG or JPEG, told apart by its leading bytes, returned as stored
  (`imagecodecs`). A file with samples is `cyx` (an RGB file is c = R, G, B), a grey file `yx`. `open_array`
  reads the header only (PNG `IHDR`, JPEG start-of-frame); `read` decodes the file once and keeps it.
- **Label form** (`draw: head` or `draw: halo`, two urls: the table and the image it annotates, in any order).
  The table is headerless text, one row per sperm, six numbers separated by `;`:

  ```
  tail X; tail Y; head X; head Y; minor-axis length of the head; maximum halo thickness
  ```

  all in pixels of the image. It becomes a `yx` `uint16` plane on the image's grid with one disc per row,
  centred on the head point and filled with the row's 1-based number in the file (blank lines are not
  counted); 0 is background. The image's header gives the grid; none of its pixels is decoded for the label.

**Why the label names its image.** A table holds coordinates only, and the harness needs a label on its raw's
grid. The dataset this was written for is served under opaque object ids, so no file name pairs a table with
an image: the binding lists both, and the snippet tells them apart by content (PNG or JPEG magic bytes against
text). The snippet cannot check that the table belongs to that image beyond every head point lying inside it;
the binding's `reasons` say why they pair.

## How the discs are drawn

- **Coordinates.** X is the column and Y the row, with pixel (row r, column c) at x = c, y = r. A pixel
  belongs to a disc when its centre is within the radius of the head point; the pixel nearest the head point
  is always painted, so every row is in the label even with a zero-length minor axis. The files do not say
  whether a whole number means a pixel's centre or its corner; read the other way every disc moves by half a
  pixel.
- **`draw: head`**: radius = half the minor-axis length. The head is longer than it is wide, so this is the
  largest circle the measurement supports inside the head, not the head's outline (the table has no major axis
  and no angle).
- **`draw: halo`**: radius = half the minor axis + the maximum halo thickness: head and halo together. Because
  the thickness is the halo's maximum, this is an upper bound of the halo, and neighbouring discs overlap.
- **Overlap.** Discs are painted largest first, so the smaller of two overlapping discs is on top (file order
  breaks ties).
- **Borders.** A disc is cut at the image border. A head point outside the image raises at open (the table of
  another image).
- **The tail point** (columns 1 and 2) is parsed but not drawn.
- **Strict table.** A line that is not six finite numbers separated by `;`, a negative length or thickness, a
  file that is not UTF-8, or more than 65,535 rows raises, naming the line. A UTF-8 byte-order mark, CRLF line
  ends and blank lines are accepted. A table without rows is an all-background plane.
- **Exif orientation.** A JPEG is returned as stored; an Exif `Orientation` is not applied. A table *with*
  rows whose JPEG has an orientation other than 1 raises: the stored pixels are turned against the displayed
  picture and the table does not say which of the two the points were marked on.

**The labels are discs, not masks**: where a head was marked and how wide it was measured, not its outline.

- **Access.** `plane` for both forms: a region of the image costs the whole file, and the label plane is
  painted whole at the first read. `io.open` is used; a server without Range raises `NeedsFetch` and the file
  is staged with `io.fetch`. Opening a label reads the table and the image's first MiB (more only when a
  JPEG's metadata is longer than that).
- **dtype.** The image's own (`uint8`; `uint16` for a 16-bit PNG); `uint16` for the label.
  **Voxel size:** `None` (JPEG and PNG densities are print sizes).

## Known limits

- One image per array, and one table with its image per label. Without names to pair by, a set of images is
  one raw and one label each.
- 8-bit grey or 3-component JPEG and 8/16-bit grey, grey+alpha, RGB or RGBA PNG. A palette or 1/2/4-bit PNG
  raises, and so does a file whose decoded shape or dtype is not what its header says. No TIFF.
- PNG `eXIf` orientation is not read.
- Only this six-column layout; the columns are not configurable.
- Tested on real data with one JPEG and its table. The PNG path, grey and 16-bit images, the `NeedsFetch`
  path, a JPEG header past the first MiB and the Exif refusal were exercised on synthetic files only: the
  record names no PNG url.

## Example

`examples/mendeley-p3ntt2z294-sperm-scd-brightfield.yaml`: image 293 of a set of 130 bright-field images of
Wright-Giemsa stained human sperm after a sperm chromatin dispersion test, a 3.2 MB RGB JPEG (2048x3072,
no Exif orientation) and its 42-row table. While testing, the label was compared pixel for pixel with an
independent raster of the same discs; the 42 head discs (366 to 1438 px each, 0.5 % of the image) do not
overlap, and with `draw: halo` 4 of the 42 discs are partly covered by a smaller neighbour (6.2 % of the
image). The coordinate frame was checked on the pixels: the green channel averages 56 inside the head discs
against 164 over the image, 108 to 125 with the discs moved 40 px along either axis, 161 to 162 with x or y
mirrored and 169 with x and y exchanged, and outlines drawn on the image sit on the dark head cores. The
example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
