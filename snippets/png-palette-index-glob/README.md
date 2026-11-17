# png-palette-index-glob

Reads a set of palette (indexed-colour) PNGs, one image per file, and presents it as one `uint8` array of the
**stored palette indices**: one file per z plane, or no z axis for a single file. It is for masks saved as
indexed PNGs, where the index is the instance or class id and the palette only says how to colour it.
`image-tiles-glob` and `png-channels` refuse such a file, because a PNG decoder replaces each index by its
palette colour (RGB, or RGBA with a tRNS chunk) and the ids are lost.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax: `*`,
  `?`, `[...]`; `*` also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are
  concatenated in binding order, and a file named twice raises. A label array bound with a glob over one folder
  (`Segmentations/gt_*.png`) pairs plane for plane with a raw array bound over a sibling folder
  (`Images/image_*.png`) when the two sorted name lists pair. The snippet cannot check that across arrays, so
  the binding's `reasons` must say why the names pair.
- **Files.** PNG colour type 3 (palette) at bit depth 1, 2, 4 or 8, interlaced or not. Anything else raises and
  names `image-tiles-glob`, which reads greyscale and colour PNGs. Every file of one array must have the first
  file's height and width; one that differs raises at `read`.
- **Values.** The palette index of each pixel as stored, `uint8`. The palette itself (PLTE) and its
  transparency (tRNS) are not read, so two indices with the same colour stay two ids, and an index beyond the
  palette's length is returned as it is.
- **How.** The index of a palette PNG and the sample of a greyscale PNG of the same bit depth are stored the
  same way: one sample per pixel, the same row filters, the same interlacing. Each file is re-wrapped in memory
  as a greyscale PNG (IHDR colour type 3 set to 0 with a new CRC, the IDAT chunks kept byte for byte, every
  other chunk dropped) and decoded with `imagecodecs`. The decoder stretches a 1/2/4-bit grey sample to 0..255
  (2-bit 0,1,2,3 reads as 0,85,170,255); the snippet divides that out exactly and raises if a decoded value is
  not a multiple of the step. The IDAT CRCs are the file's own, so the decoder still checks the pixel data.
- **Axes.** `zyx`, or `yx` with one file. **dtype** `uint8` always. **Voxel size:** `None`; a PNG's pHYs chunk
  describes print size.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. A stored member or a
  Range-capable plain URL is read through `io.open`; a deflated member (or a server ignoring Range) raises
  `NeedsFetch` and is staged whole with `io.fetch`. `open_array` reads only the first file; the other files
  are fetched when `read` asks for them, and the last decoded file is kept.
- **Small zips are staged whole.** A deflated member read by range costs two requests (local header, compressed
  bytes), and indexed masks are a few kB each. The first time a member of a zip is needed, a zip of at most
  16 MiB is fetched whole once with `io.fetch`; `io` then serves its members from that copy. So opening one
  mask of such a zip costs the whole zip (at most 16 MiB of the job's bytes). A larger zip, a zip whose size the
  server does not state, or a job that reads from its cache alone is read member by member as above.
- **Params.** None.

## Known limits

- Palette PNGs only. A greyscale or colour PNG, a JPEG or a TIFF raises; bind those with `image-tiles-glob`.
  A set that mixes palette and greyscale masks cannot be read as one array by either snippet.
- At most 256 ids per file (a palette index is one byte), and each file numbers its ids on its own: the same
  id in two z planes is not the same object.
- An APNG's later frames (fdAT chunks) are ignored; the default image (IDAT) is read.
- Each file is read and decoded whole into memory. No pyramid levels.
- A zip over 16 MiB is read by two requests per deflated member, made lazily per plane, and the harness does
  not retry a 4xx: zenodo.org answered `429 Too Many Requests` when all 229 masks of the example were read
  member by member in one loop, which is why small zips are staged whole.

## Example

`examples/zenodo-6806398-pns-cyst-nematode-cysts.yaml`: PNS-Cyst (Zenodo 6806398), 229 photographs of soil
sample extracts with sugar beet cyst nematode cysts (RGBA `uint8`, 4112x3008, about 30 MB each, in a 7.2 GB
zip) and one manually outlined instance mask per image, saved as an 8-bit indexed PNG in a 1.6 MB zip (0
background, one index per cyst). Raw is read by `image-tiles-glob` as `zcyx` (229, 4, 3008, 4112); the label
by this snippet as `zyx` (229, 3008, 4112), pairing by the number in `image_NNN.png` / `gt_NNN.png`. The
example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
