# tiff-series-glob

Reads a z-stack stored as one single-page grey TIFF per slice at plain HTTP(S) URLs, **spread over several
folders**, and presents it as one `zyx` array (`yx` for a single file). `tiff-series-http` counts a number up
inside one folder; this snippet takes one glob per folder, so a stack of 600 files kept as `1-100/` …
`501-600/` is a binding of six URLs.

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
- **`params.count`** (optional): the number of files the URLs must stand for, taken from a complete listing.
  Open fails when the indexes give another number. Set it whenever a label stack pairs with the series, so a
  folder missing from the binding or a changed listing cannot shift z silently, and say where the number
  comes from in the binding's `reasons`.
- **Files.** Each must be a single-series, single-page `YX` TIFF with the first file's shape and dtype; one
  that differs raises at `read`, naming the URL. RGB tiles belong to `tiff-zip-glob-rgb`, multi-page stacks to
  `tiff-zip-member`.
- **Access.** `open_array` reads the directory indexes (one HEAD and one GET per glob URL) and the first
  file's header. `read` opens the files of the planes it is asked for. When the first file's pixels are stored
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
- No recursive glob, no pyramid levels, no tiles, no colour, no voxel size. Only HTTP directory indexes were
  tested; a `<zip>::<glob>` URL expands through the same `io.expand` call but has not been tried.

## Example

`examples/empiar-13156-hela-stard3-fib-sem-organelles.yaml`: EMPIAR-13156, FIB-SEM of HeLa cells. The
HeLaSTARD3_FA_YA raw volume is 600 uncompressed 1092×2516 uint8 slices `image001.tif` … `image600.tif`, 100
to a folder in `HeLaSTARD3_FA_YA_Raw/1-100/` … `501-600/`, read as `zyx` 600×1092×2516 and paired with two
600-page binary label stacks (`Endosomes_ROI.tif`, `Mitochondria_ROI.tif`) read by `tiff-zip-member`. The
other two volumes of the entry have the same layout (`HeLaSTARD3_WT_Raw`, 810 slices in 4 folders;
`HeLaWT_Raw`, 993 slices in 5 folders with number 154 absent) and are not bound: the page count of HeLaWT's
label stacks is not established.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
