# nd2-http

Reads one Nikon ND2 file (NIS-Elements, the chunked "Ver3.0" container) from any URL `io` can open, as `cyx`:
one uncompressed frame with its components as `c`. Exactly one URL per array; a dataset of one field of view per
file binds each file as its own raw.

- **Container.** Parsed by the snippet with numpy and the standard library (the `nd2` package is not on the
  allowlist). An ND2 file is a run of chunks: a 16-byte little-endian header (magic `0x0ABECEDA`, name length,
  data length), a NUL-padded name ending in `!`, then the data. The file starts with the chunk
  `ND2 FILE SIGNATURE CHUNK NAME01!` (data `Ver3.0`) and its last 40 bytes are `ND2 CHUNK MAP SIGNATURE 0000001!`
  and the offset of the chunk map, which lists every chunk's name and offset. Each chunk the snippet uses is
  checked at its offset (magic, name, length within the file) before it is believed.
- **What `open_array` reads.** The signature, the chunk map, `ImageAttributesLV!` and `ImageCalibrationLV|0!`,
  and the 16-byte header of the frame's `ImageDataSeq|<frame>!` chunk: normally the first and the last 1 MiB
  block of the file (two range requests after the HEAD), no pixels. The two metadata chunks are in Nikon's
  binary "lite variant" encoding (typed, named entries, nested levels), decoded here; compressed variants and
  the XML metadata of older writers are not.
- **Shape and dtype.** From `SLxImageAttributes`: `uiComp` components, `uiHeight`, `uiWidth`, and
  `uiBpcInMemory` 8 or 16 as uint8 or uint16. The array is always `cyx`, `(uiComp, uiHeight, uiWidth)`, also for
  one component. Components are in file order; for a multi-channel acquisition they are the channels in the
  order NIS-Elements lists them, for an RGB camera they are the colour samples. Channel names are in
  `ImageMetadataSeqLV|0!` and are not read; `harness describe` does not know ND2 either, so a binding that
  states them takes them from the record.
- **Pixels.** `ImageDataSeq|<frame>!` holds an 8-byte timestamp and then `uiHeight` rows of `uiWidthBytes`
  bytes, components interleaved per pixel (y, x, c), little-endian; row padding beyond
  `uiWidth * uiComp * bytes` is dropped. Values are the stored numbers: nothing is scaled, and
  `uiBpcSignificant` (e.g. 12 significant bits in a 16-bit sample) is not applied.
- **Access.** `chunk`: `read({"c": k, "y": slice, "x": slice})` fetches rows `y0..y1` of the frame as one byte
  range through `io`'s block cache (all components and the full width of those rows, since they are interleaved)
  and slices `c` and `x` locally. A 256-row patch of a 4 x 2304 x 2304 uint16 frame is 4.7 MB. On a server
  without Range or Content-Length the file is fetched whole and the access is `file`.
- **Voxel size.** `SLxCalibration.dCalibration` (µm per pixel) of `ImageCalibrationLV|0!`, for y and x, in nm.
  `None` when the chunk is missing, `bValid` is false, the value is not a positive finite number, or `dAspect`
  is not 1 (which axis it scales is not documented, so it is not guessed). ND2 has no z size for a single
  frame, so there is no `z` key.

## Params

- `frame` (int, optional): for a file with more than one frame (`uiSequenceCount > 1`: a time series, z stack,
  multipoint or any mix), the index from 0 of the `ImageDataSeq` chunk to read. The snippet does not interpret
  the experiment loops, so it cannot say whether index 7 is a z plane, a timepoint or a stage position; the
  binding's reason must. Without it a multi-frame file is refused, with the frame count in the error. A
  single-frame file needs no param.

## Limits

- Only uncompressed frames (`eCompression` 2). Lossless (zlib) and lossy ND2 files are refused with the
  compression named; a reader for them is a new snippet.
- Only unsigned 8- and 16-bit pixels (`ePixelType` 1); 32-bit float ND2 is refused.
- Only the `Ver3.0` chunk container. The older JPEG2000-based ND2 (NIS-Elements before 3.0) and `Ver2.0`/`Ver1.0`
  containers with XML metadata are refused.
- A multi-frame file is read one frame per array; frames are not stacked into z, because whether they are z is
  not read from the file.
- A file whose chunk map is missing (an acquisition that was not closed properly) is refused; the snippet does
  not scan for chunks.
- Tested on real data with single-frame, 4-component, uint16, uncompressed files written by one NIS-Elements
  setup (the example binding). uint8, row padding, one to three components, `frame` and the refusals above were
  exercised on synthetic files only.
