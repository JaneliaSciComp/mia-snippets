# nd2-zip-glob

Reads a set of Nikon ND2 files (NIS-Elements), each holding one 2D grey image, and presents it as one array:
one file per z plane (`zyx`), or `yx` for a single file. The `nd2` package is not on the harness allowlist, so
`snippet.py` parses the container itself with `struct` and `numpy`.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are concatenated in
  binding order. z is the position in that list: it is a file index, not a depth. A label array bound with a
  glob over a sibling directory (e.g. `tiff-zip-glob-rgb` over `derived/masks/*_mask.tif`) pairs plane for
  plane only when both globs sort their files the same way. The snippet cannot check that across arrays, so
  the binding's `reasons` must say why the names pair.
- **Files.** Every file must be an ND2 "Ver3.x" chunk file (first bytes `da ce be 0a`, chunk map pointer in the
  last 40 bytes) whose `ImageAttributesLV!` says: one frame (`uiSequenceCount` 1), one component (`uiComp` 1),
  8 or 16 bits per component in memory, uncompressed (`eCompression` 2). Anything else raises, naming the
  value. All files must have the first file's shape and dtype; one that differs raises at `read`.
- **Pixels.** The frame is the `ImageDataSeq|0!` chunk: an 8-byte time stamp, then `uiHeight` rows of
  `uiWidthBytes` bytes, top row first, little-endian unsigned. The first `uiWidth` pixels of each row are
  returned as stored, `uint8` or `uint16`; nothing is scaled.
- **Voxel size.** `y` and `x` from `dCalibration` (um per pixel) of `ImageMetadataSeqLV|0!` when `bCalibrated`
  is true, as nm; otherwise `None`. It is taken from the first file. There is never a `z` size.
- **Access.** `plane` for several files: a plane is one file, so the harness draws several crops on one plane
  and reads it once, instead of one file per crop. Each file costs requests of its own (a deflated member is
  staged whole with `io.fetch` when its plane is first read, in two or three requests; a stored member or a
  Range-capable plain URL is read by range, metadata first), and a host such as Zenodo limits requests per
  minute. For one file: `chunk` when it is read through `io.open`, so `read` asks for the rows of the region
  only; `file` when `io.open` raises `NeedsFetch` (a deflated member, a server ignoring Range). `open_array`
  reads only the first file's metadata; for a deflated member that still stages that file.
- **Params.** None.

## Known limits

- One frame per file. Multi-frame ND2 (z-stacks, time series, multi-point files), multi-component (several
  channels or RGB), float and losslessly compressed frames raise at open; they need a new snippet.
- Tested on real data with 16-bit files whose rows have no padding (`uiWidthBytes` = 2 x `uiWidth`). The row
  stride is honoured, and 8-bit is accepted, but neither a padded nor an 8-bit file was at hand.
- Legacy ND2 (JPEG-2000 based, before NIS-Elements 3) is not read.
- The ND2 text metadata of the tested files mentions a 35-plane z loop of the acquisition; each file still
  holds one frame, and the snippet goes by the frame count in the attributes.
- Over HTTP each deflated member costs two or three requests (its local header, then its compressed bytes in
  pieces of 8 MiB), made when its plane is first read. The harness retries a dropped connection or a 5xx, but
  not a `429 Too Many Requests`: a host that limits requests per minute (Zenodo: 133, counted per client
  address, so shared with other jobs) can still fail a job that is itself well under the limit.

## Tested on

`zenodo-23377605-yeast-brightfield-cellpose-masks`: 64 deflated members `raw/*.nd2` of one Zenodo zip, each a
2304x2304 uint16 brightfield field at 0.1625 um/px, bound as `zyx` 64x2304x2304 with the Cellpose-SAM masks
`derived/masks/*_mask.tif` read by `tiff-zip-glob-rgb`. For the record's sample field the mean intensity under
each of the 2,777 mask labels, computed from this snippet's pixels, equals the dataset's own
`intensity_mean_brightfield` column to 5e-8 (a flipped or transposed image is off by thousands). The same file
served as a plain URL and as a stored zip member was read by range with identical pixels.
