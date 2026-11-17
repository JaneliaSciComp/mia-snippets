# image-tiles-7z-glob

Reads a set of one-image files, JPEG or PNG, out of a **7z archive** and presents it as one array: one file per z
plane, or no z axis for a single file. It is `image-tiles-glob` for the container that snippet and the harness
cannot open: `io.list` and `io.expand` read `<archive>::<glob>` as a zip, and a 7z fails there with
`BadZipFile`.

- **URL and `members`.** `urls` is one plain url, the 7z archive. `params.members` (required) is a glob over
  the archive's member names (`fnmatch` syntax, case-sensitive: `*`, `?`, `[...]`; `*` also crosses `/`); a name
  without glob characters selects that one member. The matching files are sorted by name and become the z
  planes. `<url>::<member>` is refused: the harness would treat it as a zip member (`harness stage` prefetches
  every binding url through `io.expand`). Two arrays bound with globs over sibling directories
  (`JPEGImages/*.jpg` and `SegmentationClass/*.png`) pair plane for plane when the two directories hold the
  same base names. The snippet cannot check that across arrays, so the binding's `reasons` must say why the
  names pair.
- **The archive** is staged whole with `io.fetch` when the array is opened. 7z packs files into folders
  (solid blocks), one compressed stream holding many files end to end, so a member is reached only by decoding
  its folder from the start, and there is no smaller unit to fetch. A second array on the same url is a cache
  hit (0 bytes). The header is parsed by this snippet (7zFormat.txt: signature header, packed or plain header,
  pack / unpack / substream info, file names, empty-stream flags) and the folders are decoded with the standard
  library's `lzma`. Supported: folders with a single **Copy**, **LZMA** or **LZMA2** coder, solid or not, any
  number of folders. The header's CRC-32 and every member's CRC-32 are checked.
- **Files.** JPEG, or PNG (8 or 16 bit, grey, colour or palette), told apart by the first bytes, not the name.
  Every file of one array must give the same shape and dtype as the first; one that differs raises at `read`.
- **Palette PNG.** Returned as its **stored palette indices**, `uint8`, never as palette colours: these are
  the class ids of a LabelMe / Pascal VOC `SegmentationClass` mask. The file is decoded as the grey image with
  the same IHDR and IDAT chunks (colour type 3 rewritten to 0, PLTE and tRNS dropped), so the decoder has no
  palette to apply. 1/2/4-bit palettes are scaled to 0..255 by the decoder and scaled back exactly.
- **Axes.** A file without samples (grey, palette) is `zyx`. A file with samples is `zcyx`, `c` in the file's
  sample order (R, G, B). With one file the `z` axis is dropped (`yx`, `cyx`).
- **Values** are the decoder's (`imagecodecs`), unchanged. JPEG is lossy, so a JPEG is fit for raw images, not
  for class ids. **dtype:** the file's. **Voxel size:** `None`; JPEG/PNG density fields describe print size.
- **Access.** `plane`: one file is one z plane and any region of it costs the whole file, plus decoding the
  folder up to it. `open_array` decodes up to the first matching member only (shape and dtype). `read` decodes
  the folder forward to the member it needs and keeps the still-compressed bytes of this array's other members
  it passes (up to 1 GiB per array), so reading planes in any order decodes each folder once. Past 1 GiB a
  step back to a member that was not kept decodes the folder again from its start. The last decoded file is
  kept, so the three `c` planes of one tile decode it once.

## Known limits

- The whole archive is downloaded, whatever is read; the job's byte cap must cover it. A multi-volume archive
  (`.7z.001`) is not read.
- Folders packed with anything else raise, naming the coder: a filter chain (BCJ, Delta, BCJ2), PPMd, BZip2,
  Deflate, or AES encryption (encrypted file names too). The error comes when a member of such a folder is
  read, so other folders of the same archive still read.
- A 1/2/4-bit **grey** PNG raises (the decoder rescales its values to 0..255, so stored ids would be lost);
  only palette PNGs of those depths are undone. Samples are exposed as stored: an RGBA PNG has 4 `c` planes.
  There is no param to pick or drop a sample.
- A folder is decoded sequentially: the first read of a member far into a large solid block decodes everything
  before it (the example's 265 MB block takes about 3 s; its masks sit at the end of the block).
- Each file is read whole into memory and the members an array has passed stay in memory (compressed, up to
  1 GiB), so this is for image sets of a few hundred MB, not for large volumes. No pyramid levels.
- A crop holds one `c` plane (the harness samples a plane per crop), so a colour image yields single-sample
  crops.
- TIFF members are not read (no example to test on); see `image-tiles-glob` for the zip / directory reader.

## Example

`examples/zenodo-21501303-rice-stem-cross-sections.yaml`: `dataset.7z` (210 MB, one solid LZMA2 folder of
1,372 files, header LZMA-packed) with 686 brightfield JPEGs of phloroglucinol-stained rice stem cross-sections
(2748 x 3584 RGB `uint8`) under `dataset/JPEGImages/` and 686 same-named 8-bit palette PNG masks under
`dataset/SegmentationClass/`. Raw is `zcyx` (686, 3, 2748, 3584), label `zyx` (686, 2748, 3584) with ids 0..4.
The dataset does not state which id is which class; the binding's `reasons` give the table inferred from the
masks' geometry (1 small vascular bundle, 2 cavity, 3 large vascular bundle, 4 stem, 0 outside) for a human to
confirm. The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name
the commit that adds it); a binding written by the worker names a commit that contains the snippet.
