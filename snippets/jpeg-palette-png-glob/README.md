# jpeg-palette-png-glob

Reads a set of one-image files, JPEG or **palette PNG**, and presents it as one array: one file per z plane, or
no z axis for a single file. The format is taken from each file's first bytes, not its name. It exists for
datasets whose masks are palette PNGs (colour type 3), which `image-tiles-glob` and `png-channels` refuse, and
whose masks are named after their image (`<stem>_mask.png` beside `<stem>.jpg`), so that planes can be paired by
name instead of by sorted position.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are concatenated in
  binding order, and a file named twice raises.
- **JPEG.** Read with `imagecodecs`, values unchanged. A grey JPEG is `zyx`; a colour JPEG is `zcyx`, `c` in the
  decoder's sample order (R, G, B). `palette` is refused. JPEG is lossy: fit for raw images, not for class ids.
- **Palette PNG.** Colour type 3, bit depth 1, 2, 4 or 8, interlaced or not; one `uint8` plane (`zyx`).
  `palette` is required and says which value a pixel gets:
  - `palette: index`: the stored palette index, whatever colour the palette gives it.
  - `palette: grey`: the palette colour of the pixel's index, as one grey value. Every index **the file uses**
    must have a colour with R = G = B and must be opaque (no `tRNS` alpha below 255), else decoding it raises with
    the index and its colour, so a coloured class map is never silently reduced to its red sample. Unused
    palette entries are not looked at.

  Choose `grey` when the record's label values are the colours a viewer shows (a black/white mask saved with a
  palette: 0 and 255), `index` when the indices are the class ids and the palette only colours them.
  The indices are not recovered from decoded colours (two indices may share a colour). The file's `IDAT` chunks
  are decoded by libpng under a greyscale `IHDR` of the same size and bit depth, which undoes row filters and
  interlacing and returns the samples; for a palette file the samples are the indices. libpng spreads 1/2/4-bit
  grey over 0..255, which is divided out again (exact), and a remainder raises.
- **`pair_urls`, `pair_suffix`: pairing by stem.** Without them, planes are in expansion order (sorted by name
  per glob). With `pair_urls` (the URLs, globs included, of the array this one pairs with, copied from that
  array's binding entry), plane k is the file whose stem, less `pair_suffix`, equals the stem of file k of
  `pair_urls`. A stem is the file name without directories and without its last extension. Open fails unless
  the stems pair one to one: a file without a partner on either side, a stem that does not end with
  `pair_suffix`, or two files with one stem on either side all raise, with counts and the first names. Put them
  on the label and leave the raw in glob order. The paired array must stack its files in `io.expand` order,
  URL by URL, as this snippet and `image-tiles-glob` do; this snippet sees only the URLs, not
  the other array.
- **Files of one array** must all decode to the first file's shape and dtype; one that differs raises at `read`.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` expands the globs (for `pair_urls` too: one
  more zip directory or index page, no file data) and reads the first file only, for shape and dtype and to try
  `palette` on it; the other files are fetched when `read` asks for them. The last decoded file is kept, so the
  three `c` planes of one JPEG decode once.
- **dtype.** `uint8` for palette PNG; the decoder's for JPEG (`uint8`). **Voxel size:** `None`; JFIF density and
  `pHYs` describe print size.

## Known limits

- Only JPEG and palette PNG. A grey, RGB or alpha PNG raises with its colour type and bit depth
  (`image-tiles-glob` reads those); TIFF is not read.
- `palette: grey` has no rule for a coloured or transparent palette entry in use: it raises. There is no mapping
  from colours to class ids; use `index` and say what the indices mean in the binding's `reasons`.
- Stems are compared without directories, so two paired files with the same name in different folders raise
  rather than pair; such a set needs one binding entry per folder.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour JPEG yields single-sample crops.
- Each file is read whole into memory; no pyramid levels. Over HTTP each deflated zip member costs two requests
  (local header, compressed bytes), made lazily per plane.
- Chunk CRCs of the PNG are checked by libpng for `IDAT` only; `PLTE` and `tRNS` are read as stored.

## Example

`examples/zenodo-21493260-histology-tissue-folds.yaml`: H&E teaching-slide fields with tissue-fold masks drawn
in QuPath. `images.zip` (6.6 GB) holds 2,127 colour JPEGs, 3840x2160, of which the 1,228 under
`images/<Tissue>/tissue_fold/` have a mask `masks/<Tissue>/<stem>_mask.png` in `masks.zip`. Raw is `zcyx`
(1228, 3, 2160, 3840) `uint8`; the label is `zyx` (1228, 2160, 3840) `uint8` with `palette: grey`, paired to the
raw by stem. Every mask is an 8-bit palette PNG with the same palette, black at indices 0..254 and white at
255, and stores only indices 0 and 255, so the label is 0 background, 255 tissue fold (median 5% of a mask;
2 masks are empty). The z axis is a stack of independent fields, not a volume. The example binding's
`snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds it); a
binding written by the worker names a commit that contains the snippet.
