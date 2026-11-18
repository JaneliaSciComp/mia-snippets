# tiff-tiles-canvas-glob

Reads a set of single-page TIFF tiles that **differ in height and width** as one array, one file per z plane.
The other glob snippets (`image-tiles-glob`, `tiff-zip-glob-rgb`) require every file of an array to have the
first file's shape; this one is for sets where nearly every file has its own, such as object crops cut to the
size of each object, with a same-named mask per crop in a sibling folder.

**This snippet adds pixels the files do not hold.** An array has one shape, so every plane is a canvas of
`params.canvas` (height, width). A file's pixels sit unchanged in the centre of its plane; the rest of the plane
is 0, in a raw array and in a label array alike. That is the only way a set of differently sized images fits
the `Array` contract without one binding entry per file, and it has a price: see *What the fill means*.

- **URLs.** Each URL is a glob over an HTTP directory index `<dir url>/::<pattern>`, a zip member glob
  `<zip url>::<pattern>`, a plain file or a zip member (`fnmatch` syntax). A glob is expanded with `io.expand`
  and sorted by name (as strings: `0.tif, 1.tif, 10.tif, 11.tif, ..., 2.tif`); several URLs are concatenated
  in binding order, and a file named twice raises.
- **Files.** Single-series, single-page `YX` or `YXS` TIFF, any height and width up to the canvas. All files of
  one array have the first file's dtype; another dtype raises at `read`. A file that is not a TIFF raises.
- **`canvas: [height, width]`** (required). The shape of every plane. A file of `h x w` occupies rows
  `(height - h) // 2` to `+ h` and columns `(width - w) // 2` to `+ w`; every other pixel is 0. A file taller or
  wider than the canvas raises at `read`, naming the file and its size: nothing is ever cropped. A raw and its
  labels take the same canvas, so a mask of its crop's size lands on its crop.
- **`pair: [this, other]`**. Two parts of the url, e.g. `["/crops/", "/masks/"]` on the array bound to
  `.../crops/::*.tif` and `["/masks/", "/crops/"]` on the one bound to `.../masks/::*.tif`. `this` must occur
  exactly once in every url. The url with `other` in its place is the partner, which is expanded as well, and
  a file is kept only when the partner's files hold its own url with `other` in place of `this`. Both arrays
  then hold the same file names in the same order, so z index k of the label is the mask of z index k of the
  raw, also when one listing lacks a file or holds one more (a `results.txt`, a file the other side misses).
  Without `pair` the two arrays pair by position only, and a file missing on one side shifts every later plane.
- **`skip: [names]`**. File names (the part after the last `/`) left out, for a file that cannot be read, such as
  a 0-byte file, and for its partner on the other array: give both arrays the same list. Each name must be one
  file of the array or, with `pair`, of its partner folder; an unknown name, or one that several files of the
  array carry (two folders in one array), raises. A 0-byte file that is not skipped raises at `read` and says so.
- **`count`**. The number of planes after `skip` and `pair`. Open fails on another number, so a listing that
  changed is noticed instead of shifting z.
- **`samples: grey`**. For `YXS` files whose samples are identical copies (a grey image stored as RGB): sample 0
  is returned. Samples that differ raise, so a colour image is never reduced to its red channel. A file without
  a sample axis passes through. Without the param a `YXS` file raises: the array is `zyx` and has no `c` axis.
- **`dtype: uint8 | uint16 | uint32`**. For files that hold whole, non-negative numbers in another dtype, e.g.
  label ids written as float32: the values are returned in the named dtype and checked to be the same numbers. A
  value that is not whole, is negative, NaN or infinite, or does not fit raises; nothing is rounded, clipped or
  renumbered. `harness stage` refuses a label array that is not an integer type, so a float mask needs this.
  Without the param the array has the files' own dtype.
- **Axes.** Always `zyx`, also for one file. **Voxel size:** `None` (TIFF resolution fields of such tiles
  describe print size).
- **Access.** `plane`: one file is one z plane, and any region of it costs the whole file, read through
  `io.open` (a deflated zip member or a server without Range is staged with `io.fetch`). `open_array` lists the
  urls (and the partner's, with `pair`) and reads the first file, for the dtype; the other files are fetched
  when `read` asks for them. The last plane built is kept.

## What the fill means

- The 0 around a file is not data. In a raw array it is black that the image does not contain; in a label array
  it reads as background, although nothing is known about that area. A raw file may hold real 0 pixels too, so
  the fill cannot be told from data by its value; the file's extent can be recomputed from its height and width
  only.
- The harness draws crops anywhere on the canvas. A crop therefore shows a file, or a part of it, with black
  around it, and its intensity stretch includes the fill. A crop that holds no file pixel is constant and the
  harness's content filter drops it; so is a crop whose file covers less than 5% of it.
- Keep the canvas tight: the largest height and the largest width among the array's files, no more. One large
  file makes every other plane of the array mostly fill. Files of very different sizes are better bound as
  several arrays of similar sizes (a glob with `skip` for the bulk, plain urls for the others), each with its
  own canvas.
- The planes are independent images. z is the order of the file names, not depth, and position on the canvas
  says nothing about where a tile lay in the image it was cut from.

## Choosing params

- `canvas`: measure it. The shapes are in the TIFF headers only (`harness describe <url>` per file, or read the
  files once); the snippet does not look, because that means opening every file at `open`. A canvas that is too
  small cannot give a wrong plane, only an error naming the first file that does not fit.
- `pair`, `count`: give them whenever a raw and a label array are bound to sibling folders.
- `skip`: read the directory index for 0-byte files (size `0`) before binding, on both sides.
- `dtype`: the smallest unsigned type the ids fit. The check is per file at `read`, so an id beyond the type
  further into the set raises there rather than at `open`.

## Known limits

- The fill, above. A set whose files share one shape belongs to `image-tiles-glob` or `tiff-zip-glob-rgb`,
  which add nothing.
- No `c` axis: a colour TIFF raises; only grey-as-RGB is read (`samples: grey`). TIFF only; no PNG or JPEG.
- `pair` matches whole file names: `crops/7.tif` pairs with `masks/7.tif`, not with `masks/7.png` or
  `masks/7_mask.tif`. It compares the two listings; it cannot tell whether the same-named files show the same
  thing, so the binding's `reasons` must say why the names pair.
- The canvas, the skip list and the dtype are checked at `read`, file by file, not at `open`.
- A listing is trusted as the server gives it. `ftp.ebi.ac.uk` served a `masks/` index that lacked one of its
  75 files for some minutes, and empty indexes while many were requested at once, while this snippet was
  written; `pair` then drops the file from both arrays and `count` fails the open, which is the wanted outcome
  (try again), but without `count` the array would silently be shorter.
- Each file is read whole into memory and costs a HEAD and a GET; reading every plane of a large set is many
  small requests. No pyramid levels.
- Zip members go through the same code as directory files but were exercised on synthetic data only.

## Example

`examples/s-biad3729-lewy-brain-mito-tem.yaml`: BioImage Archive S-BIAD3729, 16,845 crops of single
mitochondria cut from TEM micrographs of human brain (`<group>/Folder_NNNN/crops/N.tif`, grey stored as RGB
`uint8`, LZW) with one float32 mask per crop (`masks/N.tif`, 0 and a whole-number id; about one mask in seven
holds two or three ids). The binding takes three of the 129
folders, one raw and one label array each, 280 planes: `ENT-DLB/Folder_0038` (27 files, 21x22 to 355x389),
`Folder_0045` (180 of 182; two masks are 0-byte files) and `Folder_0046` (73 of 75; two crops are 0-byte
files). All 560 files were read through the snippet and compared with copies downloaded apart: every plane
holds its file's pixels at the stated place and nothing else, every mask has its crop's shape, all crops are
grey-as-RGB and all mask values are whole numbers. The example binding's `snippets_commit` is the commit before
this snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a
commit that contains the snippet.
