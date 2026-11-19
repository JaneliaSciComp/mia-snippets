# png-palette-glob

Reads palette PNGs (IHDR colour type 3), one image per file, and presents them as one `uint8` array: one file
per z plane, or no z axis for a single file. In a palette PNG each pixel is an index into the file's `PLTE`
colour table. A PNG decoder returns the colours, three samples per pixel, so a one-sample mask saved this way
(QuPath, PIL mode `P`, ImageJ "8-bit Color") cannot be read as ids by `image-tiles-glob` or `png-channels`, and
both refuse it. This snippet returns one plane per file by a rule the binding chooses.

- **The rule (`values`).**
  - Without the param, each pixel is its **stored palette index**, unchanged. This is right when the indices
    are the ids and the palette only colours them for display (a class map), and when index and colour are the
    same number anyway (the example).
  - `values: grey`: each pixel is the **grey level of its palette colour**. This is right when the image is a
    grey or black and white picture whose indices are arbitrary (a mask stored as index 0 = black, 1 = white
    reads as 0 and 255). Every index a file uses must have a grey colour (R = G = B); the first one that does not
    raises, with the index and the colour, so a colour picture is never reduced to its red sample. Colours of
    indices the file does not use are not looked at.
  - How to choose: `harness describe <file>` prints the palette. Indices that look like ids (0, 1, 2, ...) with
    display colours: no param. A palette of greys with indices that mean nothing: `values: grey`. State which, and
    what the palette is, in the binding's `reasons`.
- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are concatenated in
  binding order, and a file named twice raises. A label array bound by glob pairs with a raw bound by another
  glob plane for plane only when the two sorted name lists correspond. The snippet cannot check that across
  arrays, so the binding's `reasons` must say why the names pair.
- **Files.** Palette PNGs of bit depth 1, 2, 4 or 8, interlaced or not. Any other colour type raises at open
  (first file) or at `read` (later files), naming the colour type and bit depth. Every file must have the first
  file's height and width; bit depth and palette may differ from file to file (the rule is applied per file).
- **How the indices are read.** A palette PNG and a greyscale PNG of the same bit depth store their pixels
  identically; only IHDR's colour type and the `PLTE` chunk make a decoder look colours up. The snippet builds,
  in memory, a copy of the file with colour type 0 and only the `IHDR`, `IDAT` and `IEND` chunks, and decodes
  that with `imagecodecs` (libpng), so filtering, deflate and interlacing are libpng's. For 1/2/4-bit files
  libpng spreads grey values over 0..255 (x255, x85, x17) and the snippet divides them back, which is exact.
  An index beyond the file's palette (an invalid PNG) raises under either rule.
- **Axes.** `zyx` for several files, `yx` for one. **dtype.** `uint8` under either rule.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads the first file's 33-byte header only
  (height, width, colour type), no pixels; the other files are fetched when `read` asks for them. The file
  decoded last is kept, so several patches of one plane decode once.
- **Voxel size.** `None`. A PNG's optional `pHYs` chunk is a print resolution, not a pixel size.

## Known limits

- One plane per file: there is no rule that returns the palette's RGB colours as a `c` axis. A palette PNG that
  is a colour picture (a raw image) is not for this snippet.
- Transparency (`tRNS`) is ignored: an index is returned, or mapped to its grey, whatever its alpha.
- `values: grey` is checked file by file as planes are read, not at open: a file with a used colour that is not
  a grey raises at `read`, naming the file.
- A mismatched later file (size, colour type) is also found only when its plane is read.
- Chunk CRCs of the source file are not checked (the copy handed to libpng gets fresh ones); the deflate
  stream's own checksum still is.
- For an interlaced file libpng prints "Interlace handling should be turned on" to stderr; the decoded indices
  are right (tested on interlaced 1/2/4/8-bit files).
- Each file is read whole into memory. Over HTTP each deflated zip member costs two requests (local header,
  compressed bytes), made lazily per plane. zenodo.org answered `429 Too Many Requests` to some harness runs
  while this snippet was written (the limit counts requests per minute from one address); the same run
  succeeded later.

## Example

`examples/zenodo-22107895-histology-tissue-fold-he.yaml`: H&E teaching-slide images of five tissues with
tissue fold artifacts and hand-drawn (QuPath) fold masks. Raw is `image-tiles-glob` over the 1,228
`images/<Tissue>/tissue_fold/*.jpg` members of `images.zip`, `zcyx` (1228, 3, 2160, 3840) `uint8`. The label is
this snippet over the 1,228 `masks/<Tissue>/*_mask.png` members of `masks.zip`, `zyx` (1228, 2160, 3840)
`uint8`. Every mask is an 8-bit palette PNG with the same 256-entry palette, black at every index but 255,
which is white, and stores only the indices 0 and 255 (two masks hold 0 only). Index and grey level are
therefore the same number, 0 background and 255 fold, and the binding sets no `values`; `values: grey` returns
the same array. The two sorted member lists pair name by name
(`images/<Tissue>/tissue_fold/<stem>.jpg` with `masks/<Tissue>/<stem>_mask.png`) on all 1,228 planes. The
example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
