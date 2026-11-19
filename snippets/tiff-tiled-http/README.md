# tiff-tiled-http

Reads **one tiled TIFF or OME-TIFF** at a plain HTTP(S) or `s3://` URL **tile by tile**: `read` fetches and
decodes only the tiles that overlap the requested region, never the page.

It exists for whole-slide images. There one TIFF page is the whole slide (48460×58709 pixels in the example),
so the other TIFF snippets, which decode a page per read, fetch the whole file and decode gigabytes to cut one
256×256 patch. With this snippet the same patch costs the 1 MiB blocks its one to four tiles lie in.

- **URL.** Exactly one file. BigTIFF or classic TIFF; OME-TIFF or not.
- **What is read.** `series[0]` as tifffile lists it, at its **level 0** (full resolution). The pyramid's
  reduced levels and any further series (label, macro, thumbnail) are ignored. No params.
- **Axes and shape.** The series' axes with size-1 axes dropped, mapped to `czyx` (`Z`/`Q`/`I` → `z`,
  `C`/`S` → `c`). A sample axis is presented as the leading `c`: an RGB page stored `YXS` is `cyx` with
  shape `(3, Y, X)`, whether the samples are interleaved in a tile or stored as separate tile planes. A
  multi-page series (one page per channel or z plane) keeps its page axes in file order, e.g. `cyx`, `zcyx`.
  Axes that do not map to distinct `czyx` letters (a `T` axis, channels of RGB pages) are refused at open.
- **Tiles.** Each tile is decoded by tifffile's own page decoder (`TiffPage.decode`), so every compression
  and predictor tifffile and imagecodecs handle works. JPEG tiles are decoded with the page's `JPEGTables`,
  and YCbCr JPEG comes back as RGB, as `tifffile.asarray` returns it. A tile the file leaves out (byte count
  0) reads as 0.
- **dtype.** The file's, in native byte order.
- **Voxel size.** From the first OME image's `PhysicalSizeX/Y/Z` and their units; `None` for a file that is
  not OME or does not state them (plain TIFF resolution tags describe print size).
- **Access.** `chunk`: the harness asks for boxes, not planes. `open_array` reads the TIFF header, the IFDs
  of the series, the OME-XML and the first page's tile offsets (about 7 MB for each example file, whose
  offsets tables and metadata sit in different places of the file); the offsets of a further page are read
  when a region first touches it. A server that ignores Range raises `NeedsFetch`; the file is then staged
  whole with `io.fetch` and `access` is `file`.
- **Retries.** Every step that makes requests (the HEAD, the header and IFDs, a page's offsets, each tile) is
  tried up to 8 times, 0.5 to 8 s apart, when it fails with one of the HTTP client's transport errors or an
  HTTP **404**, 429 or 5xx status (told by class name; a snippet may not import the client). 404 is retried
  because the example's bucket was seen answering range requests for existing objects with 404. Blocks a
  failed try did fetch are cached, so a further try asks only for the rest. A file that really is missing
  therefore fails after about half a minute, not at once. `BudgetExceeded`, `HostNotAllowed` and the
  snippet's own errors are raised at once.

## Known limits

- Tiled pages only. A page stored in strips is refused at open (`tiff-zip-member-2` reads those, a page per
  read).
- Level 0 of series 0 only; there is no param to choose another series or level.
- No tiles with depth (`TileDepth`/`ImageDepth` > 1), no `T` axis.
- Every page of the series must be laid out like its first (tifffile's keyframe); a page that is not is
  refused when it is read. As in tifffile, the pages after the first are decoded with the first page's
  `JPEGTables`.
- `read` with a full `slice(None)` in y and x decodes the whole slide into memory; callers should ask for
  boxes, which `access: chunk` tells the harness to do.
- A region is assembled from whole decoded tiles, one after another; nothing is decoded in parallel.
- Zip members were not tested (a stored member goes through the same `io.open`).

## Example

`examples/zenodo-7637988-orion-crc-he-highplex-if.yaml`: Orion CRC (Lin et al. 2023), slide CRC02. The raw
is the registered H&E (`cyx` 3×48460×58709 uint8, 1024×1024 JPEG/YCbCr tiles, a 755 MB BigTIFF) and the
label the MCMICRO cell mask on the same grid (`yx` 48460×58709 int32 instance ids, 1024×1024 deflate tiles,
223 MB); both state 0.325 µm pixels in their OME-XML.

`harness smoke` on this binding fetched 14.0 MB (two opens of about 7 MB and one tile each); a 4-crop
`harness crop` fetched 22.4 MB in all, and the mask outlines lie on the H&E nuclei in the previews. The 404
answers the record reports did not occur in these runs; the retry was tested against a local server that
answers every other request with 404.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
