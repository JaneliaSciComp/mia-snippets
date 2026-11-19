# image-targz-member

Reads one image that is a member of a gzip-compressed tar archive (`.tar.gz`): a single-page TIFF or a PNG,
told apart by the member's first bytes. The array is `yx`, or `cyx` when the file has samples (RGB). Exactly one
URL per array, the archive's plain URL; the member is named by a param, not by `<url>::<member>`, because `io`
reads `::` as a zip member (and `harness fetch` would stage it as one). One array is one image: the images of an
archive may differ in shape (they do in the dataset tested), so they are not stacked along z. A binding lists
one raw and one label entry per pair.

- **Reaching the member.** A tar.gz has no index and cannot be read by range. The snippet opens the archive
  with `io.open` and inflates the gzip stream from its first byte, so a member costs every compressed byte up
  to its own end and nothing behind it. The compressed bytes are asked for in reads that double from 1 MiB to
  32 MiB, so the first members of a large archive cost a few MiB and a deep one is fetched by four 8 MiB range
  requests at a time. The archive is never fetched whole, unless the server has no Range or Content-Length
  (`NeedsFetch`), where `io.fetch` stages it. `io` caches the blocks, so several arrays over one archive pay
  for its bytes once; each array still inflates the stream from the start.
- **`member`** (optional): the member's full name in the tar (`./a/b.tif` and `a/b.tif` are the same name).
  Default: the first regular member whose name ends in `.tif`, `.tiff` or `.png`. Bind the member by name, and
  prefer early ones: the n-th member costs the n-1 before it.
- **Links.** A member that is a tar hard link or symlink has no bytes of its own. It is read from the member it
  points to (a hard link names its target by its full name in the archive, a symlink relative to its own
  folder), over up to 8 hops. The target lies before the link in the stream, so each hop is one more pass over
  blocks already cached.
- **TIFF.** One series of one page, `YX` or `YXS` (samples become `c`, in file order), any compression
  `tifffile` and `imagecodecs` decode. Stacks and multi-series files are refused.
- **PNG, as stored.** The array holds the samples the file stores: a palette PNG gives its palette indices (the
  class ids of a mask), a 1/2/4-bit PNG its small integers (0..1, 0..3, 0..15), grey+alpha 2 samples, RGB 3,
  RGBA 4. The palette's colours and a `tRNS` chunk are not applied. This is what `image-tiles-glob` refuses to
  do, since the decoder alone would return the palette's colours or values rescaled to 0..255. The PNG is
  decoded from its IHDR and IDAT chunks only, with a palette image declared grey (index and grey sample are
  stored alike); the decoder's exact scaling of 1/2/4-bit grey (x255, x85, x17) is undone and checked.
- **Access** is always `file`: any read needs the member inflated from the archive's start. `open_array` has to
  pass through the member to learn its shape (a TIFF's directory may lie behind its pixels), so it keeps the
  member's bytes, still encoded, and reads shape and dtype from the header; the first `read` decodes them,
  checks the result against the header and keeps the pixels.
- **dtype.** The file's (`uint8`/`uint16` for PNG), native byte order. **Voxel size:** `None`; PNG and TIFF
  resolution fields describe print size.

## Known limits

- Bytes and requests: a member deep in a large archive costs the whole archive before it. zenodo.org answered
  `429 Too Many Requests` after about 113 MB of range requests in 22 s (27 members into the 2.3 GB image
  archive tested), and the harness does not retry a 429, so on such a host only the first members are in reach
  unless the archive was staged whole before (`harness fetch`; `io.open` then reads the cached file).
- No globs and no listing: the member's name has to be known. One image per array; images are not stacked.
- Memory: the member's bytes, then its decoded pixels, are held per array. A member over 1 GiB is refused.
- Refused at open, with a message naming the cause: a member that is neither PNG nor TIFF (JPEG included), an
  interlaced (Adam7) PNG, a multi-page or multi-series TIFF, a directory or other non-file member, a missing
  member, a link chain longer than 8, a url with `::`.
- Only `.tar.gz`; a plain `.tar` or another compression is not read.

## Example

`examples/zenodo-15602048-tnbc-mitochondria-tem.yaml`: TEM micrographs of mitochondria (Zenodo 15602048) as
`tem-seg-data_slide_images.tar.gz` (2.3 GB, TIFF) and `tem-seg-data_mitochondria_masks.tar.gz` (5.2 MB, PNG),
paired by name: `data/<group>/<split>/slide_images/<name>.tif` with
`data/<group>/<split>/mitochondria/masks/<name>.png`. The binding reads the image archive's first two members
(`rwt_2_HM_1`, `rwt_3_LM_1`; uncompressed `yx` 2048x2048 `uint8`) and their masks (8-bit palette PNGs, indices
{0, 1}). Smoke fetched 8,388,608 bytes.

What the archives hold, read while this snippet was written: the mask archive has 173 PNG files and 36 hard
links, all 8-bit palette, in four shapes (2048x2048, 2048x2492, 4224x4096, 3404x5056). Its first and last
links (`Mixture/.../R20_72_G01_2913_25.png` to the `HCI-010` file, `PIM001-P/.../R20_120F_56B_42.png` to the
`Mixture` file) read identical to their targets, 4224x4096 with values {0, 1}. The image archive's first 27
members are regular files: 26 of `DRP1-KO` (4.2 MB each, one 5.1 MB) and then `HCI-010` at 17.3 MB each; the
listing stopped there on the 429 above, so the rest of its member list is not known.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
