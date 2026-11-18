# tiff-first-image-glob

Reads **the first image of each TIFF file** in a set of files and presents the set as one array: one file per
z plane, or no z axis for a single file.

It exists for files that carry a **preview after their image**. A Keyence BZ-X `.tif` holds a grey 720×960
page and then a 120×160 RGB thumbnail page, and tifffile reports the two as two series. `image-tiles-glob`,
`tiff-zip-glob-rgb`, `tiff-series-glob`, `tiff-series-glob-range`, `tiff-series-http` and `tiff-http-numbered`
all refuse a file with more than one series; `tiff-zip-member-2` reads series 0 but takes one file per array.
This snippet reads series 0 of every file and leaves the later series unread. A file with one series reads
the same way, so a set that mixes exports with and without a preview is one array.

- **URLs.** Each URL is a plain file, a glob over an HTTP directory index `<dir url>/::<pattern>`, a zip member
  `<zip url>::<member>` or a zip member glob `<zip url>::<pattern>` (`fnmatch` syntax: `*`, `?`, `[...]`). A glob
  is expanded with `io.expand` and sorted by name; the URLs are concatenated in binding order, and a file named
  twice raises.
  - **Many folders are many URLs**, one `<folder>/::<glob>` each. A directory glob matches the names of one
    index page and does not descend into sub-folders, and the snippet does not add a recursive form:
    `harness fetch` and `harness stage` expand every URL of a binding with `io.expand` themselves, so a URL
    only this snippet could resolve would fail there.
  - Write the glob so that it matches only the images (`*.tif`, `*_CH4.tif`). A sub-folder is never matched,
    whatever its name.
- **Files.** Series 0 of the file must be one page with tifffile axes `YX` (grey) or `YXS` (interleaved
  samples, e.g. RGB). A multi-page stack is refused (`tiff-zip-member-2` reads those).
- **Later series** are left unread only when each is **smaller than the image in both y and x**, which is
  what a thumbnail is. A later series as large as the image is a second image; the file is refused, with the
  series named, rather than read with part of its data dropped. The TIFF "reduced image" flag is not used: the
  Keyence thumbnail page does not set it (`NewSubfileType` 0).
- **Axes.** A grey image is `zyx`; an image with samples is `zcyx`, `c` in the file's sample order. With one
  file the `z` axis is dropped (`yx`, `cyx`). z is the position of the file in the expanded list. When the
  files are independent fields of view it is an index, not depth; say so in the binding's `reasons`.
- **Shape checks.** The array's yx shape and dtype are the first file's. Every file read is checked against
  them; one that differs raises a `ValueError` naming the URL and its position. A set with two image sizes is
  two arrays, bound by the folders or globs that separate them.
- **`params.count`** (optional): the number of files the URLs must stand for. Open fails on another number.
  Set it whenever the URLs are directory globs and the number of files is known from a complete listing (see
  "Directory listings" below), and say where the number comes from in `reasons`.
- **Values** are tifffile's, unchanged. **dtype:** the file's, in native byte order. **Voxel size:** `None`;
  plain TIFF resolution tags describe print size (the Keyence files say 96 dpi), so take it from the record.
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file. `open_array`
  resolves the globs (one HEAD and one GET per directory index, or one tail read per zip) and reads the first
  file to parse its tags; no pixels are decoded. `read` gets each file it needs whole in one request through
  `io.open` (a stored member or a Range-capable URL) or, on `NeedsFetch` (a deflated member, a server ignoring
  Range), staged with `io.fetch`. The last decoded file is kept, so the `c` planes of one file decode once.
- **A 403 is asked again.** When the host answers a listing or a file with HTTP 403, the snippet repeats that
  step after 5, 20 and 60 s and raises the fourth 403 as it is (see "Directory listings"). Nothing else is
  repeated here: `io` already retries dropped connections, 5xx and 429, a 404 is final, and so are
  `HostNotAllowed`, `BudgetExceeded` and every error of the snippet's own checks.

## Directory listings

Open costs one index page per directory, in sequence: the example's 278 folders of raw 5 took 186 s on
ftp.ebi.ac.uk, and every one of the example's 290 directories has to answer for its job to open. That host
does not always answer an index it serves. Two ways were seen, both gone a few minutes later:

- **HTTP 403.** The crop job of this snippet's first version ended on `403 Forbidden` for the index of
  `240711 E coli Plate 2 Non-confocal/Images/`, in an array `image-tiles-glob` read then. The same URL answered
  200 with its 19,200 entries three times in a row afterwards. One pass of HEAD and GET over the example's
  290 directories, made while this was written, met it in 6 of them, mostly in neighbouring requests: three
  consecutive well folders of `2025-09-30/` and one of `2025-10-27_I/` refused both requests, and two more
  of `2025-10-27_I/` refused the HEAD and answered the GET. All six answered 200 to both a few minutes later.
  `io` raises a 4xx at once (only 5xx, 429 and transport errors are retried there), so one such answer among
  290 listings fails the job. This snippet therefore repeats a refused step: `io.expand` of the URL, or the
  read of a file, after 5, 20 and 60 s. A listing that failed is not kept by `io`, so the repeat is a new
  request. A directory that is really forbidden costs 85 s before its 403 is raised. The example binds every
  array through this snippet for that reason, the single-series Opera Phenix plates included.
- **HTTP 200 with an empty index page.** 35 of the example's folder indexes (one date folder and 34 well
  folders, each of about 50 files) came back so on one pass, and the index of a plate folder once; four
  further passes agreed with the full listing. Here the snippet cannot ask again: `io` keeps a directory's
  listing for the life of the job, and in a writable dataset cache it stores it under the directory's
  validators. What it does is fail instead of reading a shorter array:
  - a glob that matches nothing is an error from `io.expand` (`no member of <dir> matches <glob>`), so an
    empty page fails the open and names the folder;
  - `params.count` fails the open when the total is not the expected one, which covers a listing that is
    short without being empty (not seen here, but nothing rules it out).

  A job that fails this way has to be run again; if a stored empty listing persists, that is a harness matter.

## Known limits

- One request per file and the file whole in memory: this is for images of a few MB, not for large ones. No
  tiles by range, no pyramid levels.
- The check on later series is by size only. A file whose first series is the preview and whose image comes
  second is refused (the second series is not smaller), not reordered.
- A file of another shape is found when it is read, not at open; reading every header at open would cost a
  request per file. Establish the shapes before binding (file sizes, `harness describe` of a file per group).
- Samples are exposed as stored (an RGBA image has 4 `c` planes). Samples stored as separate planes (tifffile
  axes `SYX`) are refused.
- No recursive glob (see URLs). A binding over many folders is long, and its listing is as of the day it was
  written: a folder added later is not read, and with `count` a file added to a bound folder fails the open.
- Only grey images over HTTP directory indexes were read on real data (`uint8` with a thumbnail, `uint16`
  without). RGB images, zip members (stored and deflated) and big-endian files were tried on synthetic files
  only, and so was the repeat after a 403: against the harness's `IO` over a mock server that refuses an index
  or a file a set number of times.
- The repeat after a 403 tells the HTTP client's status error by its class name and `response.status_code`,
  since a snippet may not import the client. If `io` raised another type for a refused request, the 403 would
  be raised at the first answer, as before.

## Example

`examples/s-biad1851-antibiotic-moa-brightfield.yaml`: BioStudies S-BIAD1851, brightfield images of *E. coli*
and *K. pneumoniae* under antibiotics, seven raws, all read by this snippet:

| raw | what                                         | folders | files | shape (z, y, x)      | dtype  |
|-----|----------------------------------------------|---------|-------|----------------------|--------|
| 0   | K. pneumoniae plates 1-4, brightfield        | 4       | 9600  | 9600 × 2160 × 2160   | uint16 |
| 1   | E. coli plates 1-4, ch4 brightfield          | 4       | 19192 | 19192 × 2160 × 2160  | uint16 |
| 2   | E. coli plates 1-4, ch1 Hoechst 33342        | 4       | 19195 | 19195 × 2160 × 2160  | uint16 |
| 3   | E. coli plates 1-4, ch2 SYTOX Green          | 4       | 19200 | 19200 × 2160 × 2160  | uint16 |
| 4   | E. coli plates 1-4, ch3 FM4-64FX             | 4       | 19194 | 19194 × 2160 × 2160  | uint16 |
| 5   | E. coli HZI (Keyence), 720×960 images        | 278     | 13788 | 13788 × 720 × 960    | uint8  |
| 6   | E. coli HZI (Keyence), 1440×1920 images      | 4       | 200   | 200 × 1440 × 1920    | uint8  |

Raws 0-4 are the Opera Phenix plates of the issue's draft binding, with its URLs: single-series TIFFs, which
`image-tiles-glob` also reads and the draft bound to it. They are bound here so that a 403 on one of their
indexes is asked again (see "Directory listings"); their counts are what the globs match in the plates'
indexes, where the E. coli channels fall short of 4 × 4800 by the names the HTTP mirror serves as empty
directories.

Raws 5 and 6 are what the snippet was written for, the third sub-dataset
`260619 E coli HZI TIFF/<date>/<well folder>/*.tif` (Keyence BZ-X810, 100x brightfield, `uint8`, LZW, each with
the thumbnail series), as two raws by image size.
Their folders and counts are from the HTTP indexes of the 10 date folders and their 282 well folders, checked
against the BioStudies file list `260619 E coli HZI TIFF.json` (13,990 `.tif`, all labelled Brightfield, and
54 `.lnk`). The two sizes show in that list's file sizes, 0.68-0.87 MB for 13,790 files and 2.5-2.8 MB for the
200 files of four folders (`2025-10-30_I/2xClavulanate`, `2025-10-30_II/2xClavulanate`, `2xRelebactam`,
`DMSO`), and `harness describe` of a file of each group gives 720×960 and 1440×1920. Two files of the list
cannot be bound: the HTTP mirror serves `2025-09-24/C3_DMSO_100x/2025-09-24_C3_100x_XY54_00001_Z001_CH4.tif`
and `2025-09-24/C4_1xLevofloxacin_100x/2025-09-25_C4_100x_XY14_00001_Z001_CH4.tif` as empty directories
(301 to `<name>/`).

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
