# czi-targz-member

Reads one Zeiss CZI z-stack that is a member of a gzip-compressed tar archive (`.tar.gz`), as `czyx`, or as
`zyx` when `channel` picks one channel. Exactly one URL per array: the archive's plain URL. The member is named
by a param, not by `<url>::<member>`, because `io` reads `::` as a zip member.

The CZI is parsed with the standard library (`tarfile`, `struct`, `xml.etree`) and numpy; no CZI package is on
the allowlist and none is needed for uncompressed grey subblocks.

- **Reaching the member.** A tar.gz has no index and cannot be read by range. The snippet opens the archive
  with `io.open` and inflates the gzip stream from its first byte, so a member costs every compressed byte up
  to its own end (rounded up to 32 MiB while loading, 1 MiB while only headers are read) and nothing behind
  it. The archive is never fetched whole, unless the server has no Range or Content-Length (`NeedsFetch`),
  where `io.fetch` stages it. `io` caches the blocks, so the second pass over the same bytes costs no network.
- **`member`** (optional): the member's full name in the tar. Default: the first member whose name ends in
  `.czi`. Bind the member by name, and prefer an early one: the n-th member costs the n-1 before it.
- **`channel`** (optional): 0-based index in the CZI's channel order (the subblocks' `C` index, the order of
  `Metadata/Information/Image/Dimensions/Channels`). The array is then `zyx`, and only that channel's planes
  are kept in memory. Without it the array is `czyx` with every channel; the harness draws crops from all of
  them, so set `channel` when only one is the raw (e.g. the transmitted-light channel of a label-free
  prediction pair) and give the reason in the binding. Out of range fails at open.
- **Axes and shape.** One full-resolution subblock per (C, Z), each one yx plane: `c` and `z` are the sorted
  `C` and `Z` starts, `y`/`x` the subblocks' size. `z` is kept even for a single plane. Pyramid subblocks are
  ignored. dtype from the pixel type: Gray8, Gray16, Gray32Float, Gray32.
- **Access** is always `file`: any read needs the member inflated from the archive's start. `open_array`
  reads headers only (file header, subblock directory, metadata XML) when the directory and metadata lie
  before the pixel data, as in the files tested; the first `read` then loads the planes and keeps them for
  later reads. When the directory or the metadata lies behind the pixel data, the one pass at open keeps the
  planes, because the stream has to be inflated through them anyway.
- **Voxel size.** `Metadata/Scaling/Items/Distance` X, Y, Z (metres) as nm; `None` when the file has none.

## Known limits

- Bytes: a member deep in an 11-56 GB archive can pass the job's byte cap (`BudgetExceeded`); only the first
  members of a large archive are in reach.
- Memory: the planes exposed (all channels, or one with `channel`) are held in memory, e.g. 566 MB for
  7x70x624x924 uint16, 81 MB for one channel.
- Refused at open, with a message naming the cause: compressed subblocks (JPEG, LZW, JPEG XR, zstd), colour
  or complex pixel types, mosaic tiles (subblocks differing in X/Y), more than one scene, timepoint or other
  index besides C and Z, a (C, Z) grid with holes or duplicates.
- Only `.tar.gz`; a plain `.tar` or a bare `.czi` URL is not read. Multi-file CZIs are not followed.
- The channel is chosen by index; the channel names are in the file's metadata XML, not exposed by the array.

## Example

`examples/allen-label-free-prediction-3d-hipsc.yaml`: Allen Institute label-free prediction training data,
13 tar.gz archives of CZI z-stacks. The binding reads the first member of the smallest paired archive
(`dic_lamin_b1.tar.gz`, 11.0 GB; member 566 MB, czyx 7x70x624x924 uint16) with `channel: 6`, the DIC
transmitted-light channel (`63X_DIC`), as zyx 70x624x924, voxel size x/y 171.96 nm, z 290 nm. Smoke fetched
301,989,888 bytes. The example binding's `snippets_commit` is the commit before this snippet's own (a binding
can't name the commit that adds it); a binding written by the worker names a commit that contains the snippet.
