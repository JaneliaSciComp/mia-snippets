# nifti-http

Reads one single-file NIfTI-1 volume, gzip-compressed (`.nii.gz`) or not (`.nii`), from any URL `io` can open
(https, `s3://` public buckets). Exactly one URL per array; a dataset with one file per channel (such as
`anterograde_tracer_C1/C2/C3.nii.gz`) binds each file as its own array.

- **Header.** The 348-byte header is parsed by the snippet, with numpy and the standard library only (nibabel
  is not on the allowlist): `sizeof_hdr` for the byte order (348 little- or big-endian), `dim`, `datatype`,
  `bitpix`, `pixdim`, `vox_offset`, `scl_slope`, `scl_inter`, `xyzt_units` and the magic, which must be `n+1`.
  Header extensions are skipped: the data starts at `vox_offset`. `open_array` reads only the header: the first
  1 MiB block of the file through `io.open`, gunzipped as far as the header when the file starts with the gzip
  magic (the file's content decides, not its name).
- **Axes.** NIfTI stores x fastest, so the file is a C-order `(z, y, x)` block and the array is `zyx` with shape
  `(dim[3], dim[2], dim[1])`; a file with `dim[0] = 2` is `yx`. nibabel shows the same file as
  `(dim[1], dim[2], dim[3])`, i.e. the transpose: voxel `[z, y, x]` here is nibabel's `[x, y, z]`. The array is
  in the file's own voxel order. The qform/sform orientation is not applied, so which anatomical direction
  x, y and z are is whatever the file's affine says.
- **Datatypes.** uint8, int8, int16, uint16, int32, uint32, int64, uint64, float32 and float64, either byte
  order, returned in native order.
- **Values.** When the header sets a scaling (`scl_slope` finite, not 0, and not the identity 1, 0), `read`
  returns `stored * scl_slope + scl_inter`, the value the NIfTI standard defines and nibabel returns. The dtype
  is then float32 for stored 8- and 16-bit integers and float32, float64 for wider types (nibabel gives float64;
  a float32 result differs from it by rounding, about 6e-8 relative). Without a scaling the stored dtype and
  numbers are returned. `stored_values: true` turns the scaling off.
- **Access.**
  - `.nii.gz`: `file`. A gzip stream cannot be read by range, so the first `read` stages the whole file with
    `io.fetch` (one request, kept in the dataset cache) and inflates it as a stream. The volume is never held in
    memory: a read inflates up to the plane it needs and keeps that one z plane, so further patches on the
    plane cost nothing and a later plane continues the stream. An earlier plane inflates again from the start
    of the file (well under a second per 100 MB of voxels).
  - `.nii`: `chunk`. `read({"z": k, "y": slice, "x": slice})` fetches rows `y0..y1` of plane `k` as one byte
    range through `io`'s block cache and slices `x` locally. On a server without Range the file is fetched
    whole and the access is `file`.
- **Voxel size.** `pixdim[1..3]` times the spatial unit in `xyzt_units` (m, mm or µm), in nm. `None` when the
  header leaves the unit unset (code 0) and the binding does not give `pixdim_unit`, or when a pixdim is 0,
  negative or not finite. pixdim is float32; its shortest decimal is used (0.05 mm gives 50000.0 nm).

## Params

- `pixdim_unit` (`m`, `mm` or `um`, optional): the unit of `pixdim` for a file whose `xyzt_units` does not
  state one. Set it only with evidence from the record or a sibling file, and say which in `reasons`. It never
  overrides the header: a file that states a different unit is refused.
- `stored_values` (bool, optional, default false): return the stored numbers in the stored dtype, ignoring
  `scl_slope`/`scl_inter`. For an integer label whose header scaling is known to be wrong; reason required.
  Stored integers of files with different slopes are not comparable with each other.

## Known limits

- Single-file NIfTI-1 only: NIfTI-2, `.hdr`/`.img` pairs (magic `ni1`) and ANALYZE 7.5 are refused.
- x, y, z only: a file with a time or vector dimension larger than 1 (`dim[4..7]`) is refused, and so are RGB
  (datatypes 128, 2304), complex, binary and float128 data. The `Backlit.nii.gz`/`Nissl.nii.gz` files of the
  example dataset are RGB24 and need a new snippet.
- Orientation is not normalised (no qform/sform reorientation, no flips), and the affine's translation and
  rotation are not reported; only `pixdim` reaches `voxel_size_nm`.
- A truncated gzip file is only noticed when a read reaches the missing part (an `EOFError` from gzip, or
  "file ends inside the voxel data"); an uncompressed file is checked against the header at open.
- Random access to a `.nii.gz` costs CPU, not bytes: planes read in descending z order each restart the
  stream. The harness reads in ascending plane order when staging.
- The `.nii` path (access `chunk`) and big-endian files were tested on synthetic files only; the example
  dataset is little-endian `.nii.gz`. Zip members are untested.

## Example

`examples/aws-brainminds-marmoset-connectivity-stpt-tracer.yaml` binds STPT channel C1 of one animal at 50 µm:
`zyx` 490 × 800 × 580, stored uint16 with `scl_slope` 0.59245, so float32 values, and `pixdim_unit: mm` because
the file's `xyzt_units` is 0. Its `snippets_commit` is the commit before this snippet's own (a binding can't
name the commit that adds it); a binding written by the worker names a commit that contains the snippet.
