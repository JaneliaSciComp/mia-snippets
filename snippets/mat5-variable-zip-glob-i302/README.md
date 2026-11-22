# mat5-variable-zip-glob-i302

Reads **one variable of each MATLAB v5 MAT-file** (`.mat`), one file per z plane: the files are zip members or
directory files chosen by glob (or listed one by one), and `key` names the variable. It exists for label maps
kept as MAT variables beside image tiles, e.g. the `inst_map` and `type_map` of HoVer-Net style nucleus
annotations, where `image-tiles-glob` reads the images.

The MAT-file is parsed in the snippet with `numpy`, `struct` and `zlib`; `scipy` is not on the allowlist. A v5
file is a 128-byte header followed by one element per variable, either a `miMATRIX` or a `miCOMPRESSED` element
holding a zlib-compressed `miMATRIX`. That is what MATLAB's `save` writes with `-v6` (uncompressed) and `-v7`
(compressed, its default), and what `scipy.io.savemat` writes. A `-v7.3` file is HDF5 and is refused here;
`hdf5-zip-member` reads those.

- **URLs.** A glob over a zip's members (`<zip url>::<glob>`) or over an HTTP directory index
  (`<dir url>/::<glob>`), `fnmatch` syntax where `*` crosses `/`; plain file urls and single members work too.
  The matches are sorted by name and become the z planes; several urls keep the binding's order. A file named
  twice raises. One file gives a `yx` array, more give `zyx`.
- **`key`** (required, the only param): the variable's name, the same in every file. Two variables of the same
  files are two entries of the binding with the same urls.
- **What a variable must be.** A real numeric array (`double`, `single`, `int8` to `uint64`) with two
  dimensions, of the same shape and class in every file. A complex, sparse, char, cell, struct or object
  variable, or one with another number of dimensions, raises at open with its class and dimensions in the
  message. A missing `key` raises with the names, classes and dimensions of the variables the file does hold.
  Variables before the one asked for may be of any class; they are skipped by their headers.
- **Axes.** Rows are `y`, columns `x`: element `(r, c)` of the MATLAB matrix is pixel `[r, c]`. MAT-files store
  columns first, and the plane is returned C-ordered.
- **Values and dtype.** The stored values, in the dtype of the variable's MATLAB class (native byte order).
  MATLAB may store a class in a narrower type when the values fit (a `double` label matrix as `uint8` bytes);
  the dtype is still the class's, here `float64`, and the values are the same. A logical array is `uint8`
  0/1.
- **Open.** `open_array` expands the urls and reads the first file: its variables' headers only. Of a
  compressed variable at most the first 4096 bytes are inflated, enough for name, class and dimensions, so
  nothing of the data is decoded. The other files are not touched until `read`.
- **Read.** `read` gets the files of the z planes it is asked for. A stored member or a Range-capable plain
  URL is read through `io.open`; a deflated member (or a server without Range) raises `NeedsFetch` and is
  staged whole with `io.fetch`. Only the variable asked for is inflated, and never past the size its
  dimensions allow. Each file is checked against the first: another shape or class raises. The last plane
  decoded is kept, so further patches of it cost nothing.
- **Access:** `plane`. One file is one plane, and any region of it costs the whole file.
- **Voxel size:** `None`; a MAT-file has no calibration.

## Known limits

- Little-endian files only (endian indicator `IM`). A big-endian file (`MI`) raises at open: none was at hand
  to test against.
- 2-D variables only. A `(rows, columns, samples)` or volume variable raises with its dimensions; so does a
  variable inside a struct or cell (only top-level variables are found by name).
- A `double` label matrix is returned as `float64`, as stored; nothing is cast to an integer type here.
- Files are paired with a raw by position in the sorted listing, so the binding's author has to check that the
  raw's glob and this one match the same names in the same order (the example's `reasons` say how).
- Every file of an array must have the first file's shape and class; sets whose files differ in shape need
  one entry per file (a single url is a `yx` array).
- The scan stops at the variable asked for: damage after it in the file is not noticed.
- A file is read into memory whole and each array reads it for itself: two variables of the same files are
  two reads of each file (from `io`'s cache after the first, so the network is asked once).

## Example

`examples/gigadb-102830-cd47-ihc-nuclei-esophageal.yaml`: CD47-IHCNuSC, 86 RGB patches (1024x1024, `uint8`)
of CD47-stained esophageal cancer sections in one zip, read by `image-tiles-glob`, and 86 MAT-files of the same
`<split>/<stem>` names in another, every member deflated (the first is 65,512 bytes, 62,415 in the zip). The
files were written by `scipy.io.savemat` with compression (`MATLAB 5.0 MAT-file Platform: posix`), and each
holds five variables in this order:

| variable | class | dimensions | |
| --- | --- | --- | --- |
| `inst_map` | `uint16` | 1024x1024 | instance ids, 0 background; bound as `unknown-nucleus-instance` |
| `inst_uid` | `int64` | 1xN | the ids in `inst_map` |
| `inst_type` | `int64` | 1xN | each instance's subtype id |
| `inst_centroid` | `int64` | Nx2 | each instance's `[x, y]` |
| `type_map` | `uint8` | 1024x1024 | subtype ids 0..7; bound as `unknown-nucleus-semantic` |

The binding reads `inst_map` and `type_map` as `zyx` `[86, 1024, 1024]` against the raw's `zcyx`
`[86, 3, 1024, 1024]`. What was checked on all 86 files, through the snippet:

- The two zips list the same 86 `<split>/<stem>` names, so sorted position pairs each annotation with its
  image.
- `inst_map`'s nonzero ids are exactly the file's `inst_uid`: 18,483 nuclei in all, ids up to 735, no file
  without nuclei. In 3 files some numbers are skipped.
- `type_map` is nonzero exactly where `inst_map` is, has one value per instance, and that value is the
  instance's `inst_type`. Ids 0 to 7 occur.
- Rows are y: the mean position of each instance's pixels is a median 0.8 px from the file's `inst_centroid`
  taken as `[x, y]`, and 427 px from it with x and y swapped. In 9 files compared with the raw, the pixels
  under the mask are darker than those outside it and than those under the transposed mask, in every file.
- `harness describe --binding` on 5 planes: every `inst_map` id is one 4-connected component, except one id of
  plane 77 with 4; `type_map` has 4 or 5 ids per plane, an id having up to 174 components.

The snippet's own reading of the format was tested with MAT-files written for the purpose (every numeric
class, compressed and not, stored and deflated members, small data elements, narrowed storage, logical and
global flags, struct, char, cell and opaque variables ahead of the one read, and the refusals above); those
tests are not part of the snippet directory. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
