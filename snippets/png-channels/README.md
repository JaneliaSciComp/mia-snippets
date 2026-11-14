# png-channels

Reads greyscale PNG files with `imagecodecs` (libpng), from plain HTTP(S) URLs or zip members, where a
multi-channel image is stored as **one grey PNG per channel**. One URL is a `yx` image; several URLs are the
channels of one field of view and are stacked as `cyx`. It has no params.

- **URLs.** One array is one field of view.
  - One URL: `yx`.
  - Several URLs: `cyx`, `c` = position in the binding's `urls`. List the channel files in channel order
    (`..._channel-0.png`, `..._channel-1.png`, …) and name the channels, when known, in the binding's
    `reasons`; a PNG does not say which stain it holds.
  - Globs are taken: a `<zip url>::<glob>` or `<dir url>/::<glob>` URL is expanded with `io.expand` to the
    matching files, sorted by name, in place (so `field_ch*.png` is channels 0..9 in order; with 10 or more
    channels, name sorting puts `ch10` before `ch2`, so list them explicitly). A plain S3 bucket has no
    directory index to glob over: give explicit URLs.
  - The same URL twice is refused.
- **Files.** Each must be an 8- or 16-bit greyscale PNG (IHDR colour type 0), interlaced or not. Refused at
  open with the colour type and bit depth in the message: RGB, palette and alpha PNGs (these are not one
  channel per file), and 1/2/4-bit grey (packed samples; not read rather than expanded to 8 bits). All
  channels of a field must have the first file's height, width and bit depth; one that differs raises at
  `read`, naming the URL. A grey PNG whose `tRNS` chunk makes the decoder add an alpha sample also raises at
  `read`.
- **Access.** Always `file`: a PNG is one deflate stream with row filters, so there are no rows, strips or
  tiles to read by range. `open_array` reads only the first file's 33-byte header (signature + `IHDR`); over
  HTTP that is the file's first 1 MiB block, cached by `io`, which for a small PNG is the file. `read`
  downloads (through `io.open`, so cached blocks are reused) and decodes each channel the region asks for,
  whole, then slices; the channel decoded last is kept, so several patches of one channel decode once. A
  deflated zip member, or a server without Range / Content-Length, raises `NeedsFetch` and is staged whole
  with `io.fetch`.
- **dtype.** `uint8` or `uint16` (native byte order), the stored sample values unchanged.
- **Voxel size.** `None`. A PNG's optional `pHYs` chunk is a print resolution, not a pixel size; take the
  voxel size from the record.

## Known limits

- **One field per array, named URL by URL.** A dataset of millions of per-channel PNGs is not enumerated by
  this snippet: a binding lists the fields it wants, each as one `raw` entry. Covering such a dataset needs
  the file list from elsewhere (an S3 `?list-type=2&prefix=` listing, or the dataset's own metadata table).
- **Only the first file is checked at open.** A missing or mismatched later channel is found when that channel
  is read (HTTP 404 from `io`, or the shape/dtype error above).
- The harness draws a crop from one channel of a `cyx` array; which channel is the sampler's choice.
- No colour, no palette, no alpha, no bit depths below 8, no voxel size, no channel names.
- Every read of a new channel decodes the whole file, however small the region.

## Example

`examples/aws-chammi-75-multichannel-pretraining.yaml`: CHAMMI-75, about 2.8 million single-channel 8-bit grey
PNGs pooled from 75 studies in the public S3 bucket `chammi-data`, one PNG per channel of a field, with shapes
and channel counts that differ by study. The binding reads two fields, the ones the record inspected: an
idr0001 field with its two channel files (`cyx` 2×1040×1376 uint8) and an hpa0018 field with its four (`cyx`
4×512×512 uint8). The channel files of each field were found by listing the field's key prefix on
`chammi-data.s3.amazonaws.com`; the bucket has no directory index, so they are explicit URLs. No labels: the
set is for self-supervised pre-training. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
