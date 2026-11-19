# tiff-targz-members

Reads TIFF members of a gzip-compressed tar archive (`.tar.gz`): one member as its own axes (`yx` for a single
greyscale page), or, with `channels`, the files of one field that differ only in a channel token of the file
name, stacked as `cyx`. Exactly one URL per array: the archive's plain URL. Members are named by params, not by
`<url>::<member>`, because `io` reads `::` as a zip member.

- **Reaching a member.** A tar.gz has no index and cannot be read by range. The snippet opens the archive with
  `io.open` and inflates the gzip stream from its first byte, 8 MiB of compressed bytes at a time, so an array
  costs every compressed byte up to the end of its last member (rounded up to 8 MiB) and nothing behind it. The
  archive is never fetched whole, unless the server answers no range requests (`NeedsFetch`), where `io.fetch`
  stages it. `io` caches the blocks, so a second array over the same archive costs no more network.
- **`member`** (optional): the member's full name in the tar, or an `fnmatch` pattern for it (`*`, `?`, `[..]`;
  `*` also matches `/`). The first regular file in archive order that matches is read. Default: the first member
  whose name ends in `.tif` or `.tiff`. Bind a member by its full name when it is known, and prefer an early
  one: the n-th member costs the n-1 before it.
- **`channels`** (optional): two or more channel tokens as the file names spell them, e.g. `[DAPI, CY5]`.
  Channel 0 is the first member that `member` selects (default: any TIFF) and that has `channels[0]` in its
  file name; channel k is the member of the same name with the last `channels[0]` of the file name (never of
  the folder) replaced by `channels[k]`. The array is `c` + the members' axes, in the order the binding lists
  the tokens. The members must agree in axes, shape and dtype. The partners may lie anywhere in the archive: the
  stream is inflated until every one was seen, and a partner that lies before channel 0 is taken in a second
  pass over the bytes `io` already cached. A partner that does not exist is only known at the archive's end.
- **`axes`** (optional): override for one member's axes, when the default below is wrong (e.g. `cyx` for a
  member whose pages are channels). Reason required in the binding.
- **Axes and shape.** A member is read from its own pages, which must all have one shape and dtype: one page
  is the page's axes (`yx`, or `yxc` for RGB samples), several pages are stacked as `z`. Size-1 axes other than
  y and x are dropped. Any compression tifffile and imagecodecs decode is read.
- **Access** is always `file`. `open_array` has to inflate the stream through the members to know their shape,
  so it keeps their encoded bytes (it does not decode pixel data); the first `read` decodes them and keeps the
  array for later reads.
- **Voxel size.** OME `PhysicalSizeX/Y/Z` from the member's own OME-XML, or ImageJ `unit`/`spacing` with
  X/YResolution, of channel 0's member; `None` when the file has neither.
- **Finding names.** `harness list` does not list a tar.gz. When no member matches, the error quotes the
  archive's first 8 TIFF member names (after reading the whole archive, or up to the byte cap). With no
  params the snippet reads the archive's first TIFF.

## Known limits

- Bytes: a member deep in a large archive can pass the job's byte cap (`BudgetExceeded`); a `member` or a
  channel partner that does not exist inflates the whole archive before the error.
- Memory: the members of one array are held in memory, encoded until the first read and decoded after it
  (e.g. 67 MB for cyx 2x2048x2048 uint16).
- A member is read alone: a multi-file OME-TIFF is not followed, and OME-XML that only points to a companion
  file (`BinaryOnly`, as in the dataset tested) gives no voxel size and no axes; the pages decide.
- Refused at open, with a message naming the cause: a member that is not a TIFF, pages of several shapes or
  dtypes in one member (e.g. a pyramid or a thumbnail page), channel members that differ in shape or dtype,
  `channels` on members that already have a channel axis.
- A multi-page member's pages are one axis (`z` by default); a hyperstack of several page axes (c and z) is
  not split.
- Only `.tar.gz`; a plain `.tar` is not read. One field per array: a binding lists one raw per field.
- The channels are chosen by file-name token; the snippet does not know what a token means.

## Example

`examples/edinburgh-datashare-8803-dna-replication-edu-dapi-dl.yaml`: Edinburgh DataShare 8803, ScanR
widefield fields of mouse ES cells, one single-page 2048x2048 uint16 TIFF per field and channel
(`...--W<well>--P<position>--Z00000--T00000--<DAPI|CY5>.tif`) in tar.gz archives. The binding reads two fields
of `mESC_annotated.tar.gz` (741,863,099 bytes), `W00054--P00037` and `W00054--P00073`, each as cyx 2x2048x2048
with c 0 = DAPI and c 1 = CY5 (EdU). The archive stores its members in no order, so the fields bound are the
first two whose both files lie near its start (members 9 to 13); in P00073 the CY5 file lies before the DAPI
file. Smoke fetched 75,497,472 bytes. The arrays were compared, pixel for pixel, with the same members
decoded by `tifffile.imread`. The files state no pixel size, so `voxel_size_nm` is `None`. The example
binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit that adds
it); a binding written by the worker names a commit that contains the snippet.
