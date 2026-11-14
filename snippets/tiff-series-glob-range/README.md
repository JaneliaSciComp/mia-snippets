# tiff-series-glob-range

Reads **a run of a numbered series** of single-page grey TIFF files at plain HTTP(S) URLs, one file per z
plane, as one `zyx` array: the files whose numbers lie between `params.first` and `params.last`.

It exists for a series whose files **do not all have one yx shape**, e.g. a FIB-SEM acquisition whose frame
was resized on the way, so that slices 1-401 are 3928×10211 and the rest a few pixels smaller. An `Array` has
one shape, so such a folder is not one array. `tiff-series-glob` and `tiff-series-http` take the first file's
shape for the whole series and fail at `read` on the first file that differs. With this snippet the binding
lists **one array per stretch of files of one shape**: the same URLs each time, and the numbers of the
stretch's first and last file. Nothing is padded, cropped or resampled; every file is read as it is stored.

- **URLs.** As in `tiff-series-glob`: each is a plain file URL or a glob over an HTTP directory index,
  `<dir url>/::<glob>` (`fnmatch` syntax, resolved by `io.expand`; one folder per glob, no descent into
  sub-folders).
  - The files of one glob are ordered by the numbers in their names, compared as numbers (`image2.tif` before
    `image10.tif`). The URLs are concatenated in binding order.
  - A file's **number** is the last run of digits in its file name (`…_s3000_highQ.tif` is 3000). The numbers
    must increase along the whole list; a folder out of place, a file without a number or a file named twice
    fails at open.
  - Write the glob so that it matches only the slices (`*_highQ.tif`, not `*`).
- **`params.first`, `params.last`** (optional): the numbers of the run's first and last file, both included.
  Files numbered outside them are left out, and z index = position among the files that are left (z 0 is file
  `first`). Each must be the number of a file the URLs stand for, so a mistyped or deleted end fails at open
  rather than moving the run. Without `first` the run starts at the series' first file, without `last` it ends
  at its last.
- **`params.count`** (optional): the number of files the run must have. Open fails on another number, so a
  file missing between `first` and `last`, or a listing that changed, is noticed instead of shifting z. Set it
  whenever a label series pairs with a raw series by position, and say where the number comes from in the
  binding's `reasons`.
- **Shape checks.** The array's yx shape and dtype are the run's first file's.
  - At open the **last file's header** is read as well; a run whose two ends differ is refused, with both
    files named. This is what catches a run laid across a shape change before any crop is drawn from it.
  - At `read`, **every file** read is checked against the array's shape and dtype; one that differs raises a
    `ValueError` naming the URL, its position in the run and its number, i.e. where to end this array and
    start the next.
  - The two ends agreeing does not prove the files between them do (a different shape in the middle is only
    found when one of those files is read). Establish the stretches before binding; see "Finding the
    stretches".
- **Files.** Each must be a single-series, single-page `YX` TIFF. A multi-page file or a colour page is
  refused (`tiff-series-glob` reads a lone multi-page stack; `tiff-zip-glob-rgb` reads RGB tiles). When the
  URLs and params leave exactly one file, the array is `yx`.
- **Access.** `open_array` reads the directory indexes (one HEAD and one GET per folder) and the headers of
  the run's first and last file. `read` opens the files of the planes it is asked for. When the first file's
  pixels are stored uncompressed in one contiguous run, `access` is `chunk` and a y range is read as that
  range of rows; otherwise `access` is `plane` and the page is decoded whole. The choice is re-made per file.
  A server that ignores Range raises `NeedsFetch` and the file is staged whole with `io.fetch`.
- **Retries.** As in `tiff-series-glob`: each step that makes requests (a directory index, a file's HEAD and
  header, a row range) is tried up to 4 times, 1, 2 and 4 s apart, when it fails with one of the HTTP client's
  transport errors (a timeout, a dropped or cut connection) or an HTTP 5xx/429 status, told by class name.
  `BudgetExceeded`, `HostNotAllowed`, a 404 and every error of the snippet itself are raised at once.
- **dtype.** The file's, in native byte order. **Voxel size:** `None`; plain TIFF resolution tags describe
  print size, not pixels. Take it from the record.

## Finding the stretches

The snippet does not look for the shape changes itself: a file's shape is in its header, and reading 6000
headers through `io` at open would cost two 1 MiB blocks per file when the IFD is at the end of the file, as
it is in the example dataset, i.e. more than a job's byte cap for every array. They are found once, when the
binding is written, and recorded in its `reasons`:

- an uncompressed single-page TIFF is its pixels plus a small header, so the **file sizes** (one HEAD per
  file, no body) fall into one group per shape; the numbers where the size jumps are the candidate ends;
- opening the binding then reads the header at both ends of every stretch, and `harness smoke` reports each
  array's shape. A wrong end shows as an open error or as a label that does not match its raw.

## Known limits

- One array per shape stretch means the series is no longer one volume: z in each array counts from its own
  first file, and a crop or a staged pyramid never spans two stretches.
- A file of another shape strictly inside a run is found at `read`, not at open (see Shape checks).
- A gap in the numbering between `first` and `last` is accepted unless `count` says otherwise.
- The numbers must increase over the whole series; a series whose numbering restarts in every folder, or
  whose files carry no number, is refused.
- The listing is read once, at open; a file that disappears afterwards is an HTTP 404 when its plane is read.
  Each read opens its file again: one HEAD per file the first time, then range requests.
- tifffile logs a warning for a private tag it cannot read (the example's files carry a vendor tag with a
  zero offset); the image tags are unaffected.
- No multi-page files, no recursive glob, no pyramid levels, no tiles, no colour, no voxel size. Only HTTP
  directory indexes were tested; a `<zip>::<glob>` URL expands through the same `io.expand` call but has not
  been tried.

## Example

`examples/empiar-12659-nicotiana-epidermal-fib-sem.yaml`: EMPIAR-12659, FIB-SEM of a *Nicotiana benthamiana*
epidermal cell. `HighQuality/` and `Mask/` each hold 6114 uncompressed single-page uint8 TIFFs
`…_s0001_highQ.tif` … `…_s6114_highQ.tif` (and `_mask.tif`), one per z slice, in three stretches:

| files         | count | shape (y × x) |
|---------------|-------|---------------|
| s0001 - s0401 | 401   | 3928 × 10211  |
| s0402 - s1197 | 796   | 3925 × 10209  |
| s1198 - s6114 | 4917  | 3925 × 10208  |

So the binding has three raws over `HighQuality/::*_highQ.tif` and three binary labels over
`Mask/::*_mask.tif`, each label paired with the raw of the same stretch. The stretches come from a HEAD of
every file in both folders (sizes in three groups, changing only at s0402 and s1198, the same in both
folders) and the six end headers the snippet reads at open. In 8 test crops drawn from all three stretches
the raw was zero exactly where the mask was background, as the record says of this dataset.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
