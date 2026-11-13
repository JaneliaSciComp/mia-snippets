# image-tiles-glob

Reads a set of one-image files, PNG, JPEG or single-page TIFF, and presents it as one array: one file per z
plane, or no z axis for a single file. The format is taken from each file's first bytes, not its name, so one
glob may mix `.tif` and `.jpg` tiles. It adds two things to `tiff-zip-glob-rgb`: PNG and JPEG (that snippet
reads TIFF only), and colour. A file with samples (RGB) is returned as a `c` axis, `zcyx`, instead of being
collapsed to one plane.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax: `*`,
  `?`, `[...]`; `*` also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are
  concatenated in binding order, and a file named twice raises. Two arrays bound with globs over sibling
  directories (`Images/*.tif` and `Masks/*.png`) pair plane for plane when the two directories hold the same
  base names. The snippet cannot check that across arrays, so the binding's `reasons` must say why the names
  pair.
- **Files.** PNG (8 or 16 bit, grey or colour), JPEG, or a single-series, single-page `YX` / `YXS` TIFF. Every
  file of one array must give the same shape and dtype as the first; one that differs raises at `read`.
  Multi-page TIFF stacks belong to `tiff-zip-member`.
- **Axes.** A file without samples is `zyx`. A file with samples is `zcyx`, `c` in the file's sample order
  (R, G, B). With one file the `z` axis is dropped (`yx`, `cyx`).
- **`samples: grey`** (the only param). For files whose samples are identical copies: a class map or grey image
  saved as RGB. Sample 0 is returned and the array has no `c` axis. Samples that differ raise, so a colour image
  is never silently reduced to its red channel. A file without a sample axis passes through, so a set that mixes
  grey and grey-as-RGB files reads as one `zyx` array.
- **Values** are the decoder's (`imagecodecs` for PNG and JPEG, `tifffile` for TIFF), unchanged: a mask PNG
  returns its stored ids. JPEG is lossy, so a JPEG is fit for raw images, not for class ids.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads only the first file (shape, dtype, and
  the param is tried on it); the other files are fetched when `read` asks for them. The last decoded file is
  kept, so reading the three `c` planes of one tile decodes it once.
- **dtype.** The file's. **Voxel size:** `None`; PNG/JPEG/TIFF resolution fields describe print size.

## Known limits

- A palette PNG or a 1/2/4-bit grey PNG raises. The decoder turns palette indices into RGB colours and rescales
  low bit depths to 0..255 (a 2-bit 0,1,2,3 reads as 0,85,170,255), so the stored ids would be lost.
- Samples are exposed as stored: an RGBA file has 4 `c` planes, the alpha among them; a PNG with a tRNS chunk
  gains an alpha sample. There is no param to pick or drop a sample; `tiff-zip-glob-rgb` has `sample: <k>` for
  TIFF.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour tile yields single-sample crops.
- Each file is read whole into memory, so this is for tiles, not for large images. No pyramid levels.
- Over HTTP each deflated zip member costs two requests (local header, compressed bytes), made lazily per
  plane; reading every plane of a large set is many small requests, and the harness does not retry.
  data.4tu.nl answered `502 Bad Gateway` to about 2% of the member reads (35 of the 1,582 masks) while this
  snippet was written; the same reads succeeded on a second run.

## Example

`examples/4tu-bf11171a-carbon-fibre-composite-masks.yaml`: a 615 MB zip of 256x256 micrograph tiles of carbon
fibre composite cross sections (RGB `uint8`, TIFF and JPEG) with same-named PNG masks (`uint8`, 0 matrix,
1 fibre, 2 void). Raw is `zcyx` (1582, 3, 256, 256), label `zyx` (1582, 256, 256). The binding takes the
Original data sets I to III and the Test data set, and binds sets II and III by file-name prefix because they
repeat files of the earlier sets (same name and CRC-32 in the zip directory). The Augmented data sets are not
bound; their masks are the same ids saved as RGB PNGs, which is what `samples: grey` reads. The example
binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds
it); a binding written by the worker names a commit that contains the snippet.
