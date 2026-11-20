# tiff-csv-index-shape-i247

Reads a folder of independent single-page grey TIFF images through the CSV table that names them, and presents
the images of **one width and height** as one `zyx` array, one file per z plane (`yx` when there is one file).
It is for a collection that the other TIFF snippets cannot bind:

- the folder has **no directory index**, so no glob can list its files (`io.list` of a Hugging Face
  `resolve/<revision>/<folder>/` is a 404);
- the images **differ in shape**, so they are not one stack; and
- the file numbers have gaps, so no numbered range (`tiff-series-http`) stands for them.

The table is the index, `width` and `height` select its rows of one shape, and a binding has one raw entry per
shape. z has no spatial meaning: plane k is the k-th selected row of the table, an image of its own.

## URLs

One or two plain URLs per array, in any order:

- **The table**, the URL that ends in `.csv`. A header row, then one row per image, with the columns
  `file_name`, `width`, `height` (the layout of a Hugging Face `metadata.csv`; other columns are ignored).
  `file_name` is the image's path relative to the table's own folder, so the image's URL is the table's URL
  with its last segment replaced by `file_name` (percent-encoded). `width` is the x size and `height` the
  y size in pixels. A name that leaves the folder (`..`, a leading `/`, an empty segment, a backslash) is refused.
- **The listing** (optional), the other URL: a Hugging Face repo-info document,
  `https://huggingface.co/api/datasets/<owner>/<repo>` (or `.../revision/<revision>`), a JSON object whose
  `siblings` are `{"rfilename": <path in the repository>}`, one per file of the repository, all in one
  response. With it, **a row whose file the listing does not hold is left out**. Give it whenever the table
  names files that are not there; without it every row is taken to exist, and a missing file is a 404 at the
  `read` of its plane. The table must then be at a `.../resolve/<revision>/<path>` URL, which is how the
  snippet knows where the table's folder lies in the repository. Open fails when the listing holds none of
  the table's files.

Both documents are staged whole with `io.fetch` (they are small: 2.1 MB and 0.5 MB for the example) and
cached by `io`, so the second and later arrays of a binding cost no more network for them.

## Params

- `width`, `height` (required, integers): the shape to read, as the table spells it. The array is the rows
  with exactly this `width` and `height`, in the table's row order, minus those the listing leaves out. Take
  the shapes and their counts from the table and the listing, and say in the binding's `reasons` which shapes
  are bound and which are not. When no row matches, the error names the eight most frequent shapes and their
  row counts.

## What it returns

- **Axes and shape.** `zyx`, `(files, height, width)`; `yx` for a single file.
- **dtype.** The first file's, in native byte order. A later file of another dtype raises at `read`.
- **Files.** Each must be a single-series, single-page `YX` TIFF whose page is `(height, width)`. The first is
  checked at open, each other one when its plane is read; a file that is not the shape its row says raises,
  naming the URL, rather than being cut or padded.
- **Access.** `open_array` reads the table, the listing and the first file's header. `read` opens only the
  files of the planes it is asked for. When the first file's pixels are stored uncompressed in one contiguous
  run, `access` is `chunk` and a y range is read as that range of rows by byte range (a 256-row patch of a
  2048×2048 float32 image is 2 MB, not the 16.8 MB file); otherwise `access` is `plane` and the page is
  decoded whole. The choice is re-made per file, so a compressed file inside a raw stack still reads correctly.
  A server that ignores Range raises `NeedsFetch` and the file is staged whole.
- **Values** are the file's, unchanged. The images of one stack are unrelated acquisitions and may differ
  widely in range (the example's per-image maxima run from hundreds to millions), so nothing common can be
  assumed about their normalisation.
- **Voxel size.** `None`; plain TIFF resolution tags describe print size, not pixels.

## Known limits

- **Only the table and the listing are in the binding's `urls`.** The harness derives what it knows about
  sources from `urls`: a crop's `crop.json` carries the validators of those two documents (which fix what each
  z index means), and `harness stage` prefetches and records in `derived_from` only them; the images are then
  read by range through `io` (counted, host-checked, their validators in `io.validators`), and an offline
  re-stage from the cache cannot reach them. This is the limit `tiff-series-http` has for its numbered files.
- **z depends on the table and the listing.** If the repository gains or loses files, or the table changes,
  the same z names another image. A repo-info document also carries the repository's download counter,
  so its validators can change while its `siblings` stay the same. Pin both URLs to a commit
  (`resolve/<sha>/...`, `.../revision/<sha>`) when that matters.
- The listing is Hugging Face's repo-info JSON only; another host's listing needs another parser. A
  repository so large that the hub stops returning every file in `siblings` would lose rows silently; compare
  the array's z size with a file listing, as the example's reasons do.
- The table's `width` and `height` are trusted for selection and checked against each file only when it is
  read; `dtype` and the statistics columns of the table are not read at all.
- Shapes with few files make small stacks, and one binding entry each; a collection with hundreds of rare
  shapes is bound only in part.
- No multi-page files, no colour, no tiles, no pyramid levels, no voxel size, no zip members, no tar members.

## Example

`examples/hf-stemson-ai-warwick-stem-images.yaml`: the Warwick STEM dataset on Hugging Face
(`Stemson-AI/Warwick-STEM`), uncompressed float32 STEM images of many shapes. `metadata.csv` has 19,769 rows,
`images/warwick_stem_00000.tif` … `19768.tif`, but only 9,998 of those files are in `images/`, scattered
through the numbering (4,930 separate runs), which is why the listing is bound beside the table. The nine
shapes with 47 or more files are bound, 9,454 of the 9,998 files:

| width × height | files | array |
|---|---|---|
| 2048 × 2048 | 4,017 | zyx 4017 × 2048 × 2048 |
| 1024 × 1024 | 3,358 | zyx 3358 × 1024 × 1024 |
| 512 × 512 | 1,560 | zyx 1560 × 512 × 512 |
| 256 × 256 | 117 | zyx 117 × 256 × 256 |
| 1024 × 512 | 113 | zyx 113 × 512 × 1024 |
| 2004 × 1336 | 104 | zyx 104 × 1336 × 2004 |
| 512 × 1024 | 86 | zyx 86 × 1024 × 512 |
| 2048 × 1024 | 52 | zyx 52 × 1024 × 2048 |
| 1024 × 1025 | 47 | zyx 47 × 1025 × 1024 |

Not bound: the other 544 files of `images/`, which have 464 further shapes of at most 12 files each, and the
9,771 images that exist only inside the 170 tar shards (`train-NNNN.tar`), which this snippet does not read.

Checked on the real files: for three planes of each stack (the first, the last and one drawn at random; 26
planes in all) the whole plane read through the snippet has exactly the `min_value` and `max_value` of its
table row and its `mean_value`, and a 100×100 box read by row range equals the same box of the whole plane.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
