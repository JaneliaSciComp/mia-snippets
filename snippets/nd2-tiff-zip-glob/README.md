# nd2-tiff-zip-glob

Reads a set of Nikon ND2 files (NIS-Elements), each holding one 2D grey image, and presents it as one array:
one file per z plane (`zyx`), or `yx` for a single file. The `nd2` package is not on the harness allowlist, so
`snippet.py` parses the container itself with `struct` and `numpy`.

It reads the label side of such a dataset too: a set of single-page YX TIFF masks, one per ND2 file, stacked
the same way. That is not for the format's sake (`tiff-zip-glob-rgb` reads the same masks), but so that both
arrays of a binding wait out a host's `429 Too Many Requests` (see **Rate limits**), which only helps a job
when every request of the job does it.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob
  `<zip url>::<pattern>` or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax; `*`
  also crosses `/`). A glob is expanded with `io.expand` and sorted by name; several URLs are concatenated in
  binding order. z is the position in that list: it is a file index, not a depth. A label array bound with a
  glob over a sibling directory (e.g. `derived/masks/*_mask.tif`) pairs plane for
  plane only when both globs sort their files the same way. The snippet cannot check that across arrays, so
  the binding's `reasons` must say why the names pair.
- **Files.** A file is taken as TIFF when it starts with a TIFF or BigTIFF signature, else as ND2; all files
  of one array must be of one kind. Every ND2 file must be a "Ver3.x" chunk file (first bytes `da ce be 0a`,
  chunk map pointer in the last 40 bytes) whose `ImageAttributesLV!` says: one frame (`uiSequenceCount` 1),
  one component (`uiComp` 1), 8 or 16 bits per component in memory, uncompressed (`eCompression` 2). Anything
  else raises, naming the value. A TIFF must hold one series of axes `YX` (one page, no sample axis), of a
  numeric type; it is decoded by tifffile and returned as stored. All files must have the first file's shape
  and dtype; one that differs raises at `read`.
- **Pixels.** The ND2 frame is the `ImageDataSeq|0!` chunk: an 8-byte time stamp, then `uiHeight` rows of
  `uiWidthBytes` bytes, top row first, little-endian unsigned. The first `uiWidth` pixels of each row are
  returned as stored, `uint8` or `uint16`; nothing is scaled.
- **Voxel size.** `y` and `x` from `dCalibration` (um per pixel) of `ImageMetadataSeqLV|0!` when `bCalibrated`
  is true, as nm; otherwise `None`. It is taken from the first file. There is never a `z` size. A TIFF array
  reports `None`: a mask is on the grid of the image it labels.
- **Access.** `plane` for several files: a plane is one file, so the harness draws several crops on one plane
  and reads it once, instead of one file per crop. Each file costs requests of its own (a deflated member is
  staged whole with `io.fetch` when its plane is first read, in two or three requests; a stored member or a
  Range-capable plain URL is read by range, metadata first), and a host such as Zenodo limits requests per
  minute. For one ND2 file: `chunk` when it is read through `io.open`, so `read` asks for the rows of the region
  only; `file` when `io.open` raises `NeedsFetch` (a deflated member, a server ignoring Range). One TIFF is
  always `file`: its page is decoded whole. `open_array`
  reads only the first file's metadata; for a deflated member that still stages that file.
- **Rate limits.** Zenodo allows a client address 133 requests a minute (`x-ratelimit-limit`), counts every
  HEAD and range request, and answers the next one with `429 Too Many Requests` and a `Retry-After` of the
  seconds until the minute ends. The address is shared by every job behind it, so a job of 30 requests can be
  refused on its sixth. The harness retries dropped connections and 5xx, not 429, and the job dies. Here every
  call that can reach the network (`io.expand`, opening the first file, reading the rows of a file) is made
  again when it fails on status 429: after `Retry-After` plus one second (60 s when the header is missing, at
  most 120 s), up to 8 tries per call. Any other error, and the eighth 429, is raised unchanged. The 429 is
  recognised by the `response.status_code` of the exception `io` lets through; the harness's HTTP error type
  is not part of its API, so nothing of it is imported. A file whose fetch is repeated is asked for again
  from its first byte, and those bytes count against the job's byte cap again.
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
  pieces of 8 MiB), made when its plane is first read.
- Waiting for a rate limit takes time: up to 2 minutes per wait and 7 waits per call. A host that keeps
  answering 429 beyond that fails the job as before. Only 429 is waited for; other refusals are final.
- The TIFF side reads what tifffile decodes without `imagecodecs` (uncompressed, as tested, or packbits);
  multi-page, RGB and multi-series TIFFs raise; `tiff-zip-glob-rgb` and `tiff-zip-member` read those.

## Tested on

`zenodo-23377605-yeast-brightfield-cellpose-masks`: 64 deflated members `raw/*.nd2` of one Zenodo zip, each a
2304x2304 uint16 brightfield field at 0.1625 um/px, bound as `zyx` 64x2304x2304 with the Cellpose-SAM masks
`derived/masks/*_mask.tif` (64 deflated uint32 TIFFs) read by this snippet as a second array. For the
record's sample field the mean intensity under
each of the 2,777 mask labels, computed from this snippet's pixels, equals the dataset's own
`intensity_mean_brightfield` column to 5e-8 (a flipped or transposed image is off by thousands). The same file
served as a plain URL and as a stored zip member was read by range with identical pixels.
A 16-crop job in which every fourth HTTP request was answered with a synthetic 429 (`Retry-After: 1`), 12 of
51 requests, wrote all 16 crops; without the waiting the same job dies on the first one.
