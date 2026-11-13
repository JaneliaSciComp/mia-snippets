# tiff-series-glob

Reads a z-stack of grey TIFF pages at plain HTTP(S) URLs as one `zyx` array, in either of two layouts:

- **a series**: one single-page TIFF per slice, **spread over several folders**. `tiff-series-http` counts a
  number up inside one folder; this snippet takes one glob per folder, so a stack of 600 files kept as
  `1-100/` … `501-600/` is a binding of six URLs;
- **a stack**: when the URLs stand for exactly one file and it has several pages, each page is a z plane (the
  label stacks that sit beside such a series). A lone single-page file is `yx`.

It is written for a slow server: every step that makes requests is **retried** when it fails in transit (see
Retries), and a stack file is read in a few large requests instead of one per IFD.

- **URLs.** Each is a plain file URL or a glob over an HTTP directory index, `<dir url>/::<glob>` (`fnmatch`
  syntax, resolved by `io.expand`; a directory glob lists one folder and does not descend into sub-folders,
  hence one URL per folder).
  - The files of one glob are ordered by the numbers in their names, compared as numbers (`image2.tif` before
    `image10.tif`), not by `io.expand`'s plain name order.
  - The URLs are concatenated **in binding order**. z index = position in that list, not the file's number, so
    a missing number is not a hole: the planes after it move up by one.
  - Order check: the last run of digits in each file name is its number, and the numbers must increase along
    the whole list. A folder out of place, a file without a number or a file named twice fails at open.
  - Write the glob so that it matches only the slices (`image*.tif`, not `*`); every match becomes a plane.
- **`params.count`** (optional): the number of z planes the URLs must stand for, taken from a complete
  listing: the number of files of a series, or the number of pages of a stack. Open fails on another number. Set it whenever a label stack pairs with the series, so a
  folder missing from the binding or a changed listing cannot shift z silently, and say where the number
  comes from in the binding's `reasons`.
- **Files of a series.** Each must be a single-series, single-page `YX` TIFF with the first file's shape and
  dtype; one that differs raises at `read`, naming the URL. RGB tiles belong to `tiff-zip-glob-rgb`.
- **A stack.** One file, one tifffile series with axes `ZYX`, `IYX` or `QYX` and one page per plane (a plain
  multi-page TIFF; uniform pages). Anything else (colour, several series, an ImageJ hyperstack with one IFD
  for many planes) is refused at open; `tiff-zip-member` reads those, and stacks inside a zip.
  - tifffile visits every IFD of a plain multi-page TIFF at open, and writers put them between the pages, so
    through `io.open` the walk costs one request per 1 MiB block of the file. A stack of up to 64 MiB is
    therefore read whole first, in range requests of 8 MiB that land in the job's block cache; after that the
    open and every page read cost no further request. A larger stack is read by range as tifffile asks.
  - `access` is `plane`: a page is decoded whole.
- **Retries.** The harness's HTTP client waits 60 s for an answer and does not retry. Each step of this
  snippet that makes requests (a directory index, a file's HEAD and header, a row range, an 8 MiB run of a
  stack) is tried up to 4 times, 1, 2 and 4 s apart, when it fails with one of the client's transport errors
  (a timeout, a connection dropped or cut inside a body) or an HTTP 5xx/429 status. The errors are told by
  class name, as a snippet may not import the client. `BudgetExceeded`, `HostNotAllowed`, a 404 and every
  error of the snippet itself are raised at once. Bytes of a failed attempt still count against the job's cap;
  blocks already in the job's cache are not asked for again.
- **Access of a series.** `open_array` reads the directory indexes (one HEAD and one GET per glob URL) and the
  first file's header. `read` opens the files of the planes it is asked for. When the first file's pixels are stored
  uncompressed in one contiguous run, `access` is `chunk` and a y range is read as that range of rows;
  otherwise `access` is `plane` and the page is decoded whole. The choice is re-made per file, so a compressed
  file inside a raw series still reads correctly. A server that ignores Range raises `NeedsFetch` and the file
  is staged whole with `io.fetch`.
- **dtype.** The file's, in native byte order. **Voxel size:** `None`; plain TIFF resolution tags describe
  print size, not pixels (and an ImageJ default of 1 px = 1 micron is no calibration). Take it from the record.

Because every file is named by a URL the harness can expand itself, `harness fetch` and `harness stage`
prefetch the whole series and an offline re-stage finds every slice in the cache, which `tiff-series-http`
with `last` cannot offer.

## Known limits

- The numbers must increase over the whole series. A series whose numbering restarts in every folder, or whose
  files carry no number, is refused; it needs explicit ordering this snippet does not have.
- A gap in the numbering is accepted without notice (z is the position). `count` checks the number of files,
  not which numbers they carry: check the listing for gaps before pairing labels by page index.
- The listing is read once, at open. A file that disappears afterwards is an HTTP 404 from `io` when its plane
  is read.
- Each read opens its file again: one HEAD per file the first time, then range requests. A server that is slow
  to connect makes open slow in proportion to the number of glob URLs.
- Retries cover what goes through this snippet only, and four tries of a request: a server that is down for
  minutes still fails the job. A series of multi-page files is not read (several files means one page each).
- No recursive glob, no pyramid levels, no tiles, no colour, no voxel size. Only HTTP directory indexes were
  tested; a `<zip>::<glob>` URL expands through the same `io.expand` call but has not been tried.

## Example

`examples/empiar-13156-hela-stard3-fib-sem-organelles.yaml`: EMPIAR-13156, FIB-SEM of HeLa cells. The
HeLaSTARD3_FA_YA raw volume is 600 uncompressed 1092×2516 uint8 slices `image001.tif` … `image600.tif`, 100
to a folder in `HeLaSTARD3_FA_YA_Raw/1-100/` … `501-600/`, read as `zyx` 600×1092×2516 and paired with two
600-page binary label stacks (`Endosomes_ROI.tif`, `Mitochondria_ROI.tif`, plain multi-page LZW TIFFs of
22 MB) read by this snippet as stacks. `ftp.ebi.ac.uk` took 12-35 s to answer each request when this was
written and timed some out, which is what the retries and the whole-stack read are for. The
other two volumes of the entry have the same layout (`HeLaSTARD3_WT_Raw`, 810 slices in 4 folders;
`HeLaWT_Raw`, 993 slices in 5 folders with number 154 absent) and are not bound: the page count of HeLaWT's
label stacks is not established.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
