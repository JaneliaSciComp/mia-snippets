# png-rgb-colour-class-ids

Reads colour-coded mask PNGs, 8-bit RGB files in which each class is painted in one colour, and presents them
as one `uint8` array of **class ids**: one integer `yx` plane per file, one file per z plane, or no z axis for a
single file. The id of a pixel is the position of its colour in the binding's `colours` table.
`image-tiles-glob` reads such a file as its R, G, B samples on a `c` axis, so a label crop is one colour sample
and two classes that share a red value are the same value; its `samples: grey` refuses the file because the
samples differ; `png-palette-index-glob` reads palette PNGs only.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax: `*`,
  `?`, `[...]`; `*` also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are
  concatenated in binding order, and a file named twice raises. A label array bound with a glob over one folder
  pairs plane for plane with a raw array bound over a sibling folder when the two sorted name lists pair. The
  snippet cannot check that across arrays, so the binding's `reasons` must say why the names pair.
- **Files.** PNG colour type 2 (RGB) at bit depth 8, interlaced or not. Anything else raises at `open_array`
  (first file) or `read` (the others), and the error names the snippet for palette and greyscale masks. Every
  file of one array must have the first file's height and width; one that differs raises at `read`. Files of
  different sizes are separate arrays (one `labels` entry each).
- **`colours`** (required, the only param). The colour table: a list of `[r, g, b]` colours, integers 0..255,
  at most 256, none twice. A pixel of `colours[k]` is read as id `k`. The first colour is id 0, which the
  harness treats as unlabelled, so put the background (or "not annotated") colour first. The table is given by
  the binding and not collected from each file, so a colour has the same id in every file and every array bound
  with the same table, whichever colours one file happens to hold.
- **A colour that is not in the table raises** at `read`, and the error lists each such colour with its pixel
  count (up to 16 colours; a file with more is reported as not being a one-colour-per-class mask, e.g. a
  photograph or a mask with anti-aliased edges). Nothing is mapped to a nearest colour or to a catch-all id.
  This is also how to get the table of a dataset that does not document it: bind with the colours known so far,
  read, and append the colours the error names. Appending keeps the ids already given.
- **Values.** `uint8` ids `0 .. len(colours) - 1`. What each id means is the binding author's to state in
  `reasons`: the snippet knows colours, not class names.
- **Axes.** `zyx`, or `yx` with one file. **dtype** `uint8` always. **Voxel size:** `None`; a PNG's pHYs chunk
  describes print size.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. `open_array` reads
  the first file's header only (the first 33 bytes, which costs the file's first 1 MiB block over HTTP) and no
  pixels; `read` fetches and decodes the file it needs and keeps the last file's ids, so the crops of one plane
  decode it once. A stored member or a Range-capable plain URL is read through `io.open`; a deflated member (or
  a server ignoring Range) raises `NeedsFetch` and is staged whole with `io.fetch`.
- **Memory.** A file is decoded whole (3 bytes per pixel) and mapped through a 2^24-entry lookup table (32 MB)
  in strips of about 4 million pixels; the ids kept are 1 byte per pixel. Reading the example's 2909x14903 mask
  (130 MB of samples) took the process to a peak of 316 MB resident.

## Known limits

- 8-bit RGB only. An RGBA mask (colour type 6), an RGB mask with a tRNS chunk, a 16-bit mask, and colour-coded
  JPEG or TIFF masks raise. A palette PNG's ids are its stored indices: bind it with `png-palette-index-glob`.
  A greyscale mask's ids are its values: bind it with `image-tiles-glob`.
- Exact colours only. A mask with anti-aliased or JPEG-blurred class borders has many in-between colours and
  raises; there is no tolerance and no "other" id.
- One colour per id: two colours cannot share an id, and at most 256 colours.
- The table has to be complete for every file bound, and the snippet finds a missing colour only when it reads
  the file that holds it. A binding over files nobody has read can therefore fail at `read`, on that plane.
- Ids are positions in the binding's table, so two bindings with differently ordered tables give different ids
  for the same class. Keep one table per dataset.
- Each file is read and decoded whole into memory. No pyramid levels.

## Example

`examples/mendeley-vccj8mp2cg-histo-seg-skin.yaml`: Histo-Seg (Mendeley Data vccj8mp2cg v2), 38 H&E whole-slide
images of skin biopsies with one colour-coded RGB mask each. The binding covers the one pair the record gives
URLs for, `MD22-04169(B1-4)`: raw by `image-tiles-glob` as `cyx` (3, 2909, 14903), label by this snippet as
`yx` (2909, 14903), ids 0..7.

The record does not give the colour-to-class table (it says 12 classes), so the table is the 8 colours counted
in this mask, `[0, 0, 0]` first and the rest sorted by (r, g, b); the mask holds exactly these 8 colours, with
no in-between colours at class borders. Mapping the ids back through the table reproduces the file's pixels
exactly. Which tissue class each id is stays unknown until the dataset's legend is documented. Seen on the
sample (an observation, not a legend): `[0, 0, 0]` (id 0, 71% of the pixels) is the slide outside the tissue,
and `[224, 224, 224]`, `[112, 48, 160]`, `[0, 156, 255]`, `[0, 0, 128]` (ids 7, 4, 2, 1) are bands that follow
the skin's layers from the surface inward. The other 37 masks have no URL on the record and were not read;
they will hold colours this table lacks, and binding them means appending those.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
