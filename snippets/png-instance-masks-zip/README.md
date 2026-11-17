# png-instance-masks-zip

Reads instance labels stored as one binary mask PNG per object, `<image>_<k>.png`, inside a zip, and presents
them as one array of instance ids: one plane per image, the images stacked along z (the **label form**). It
exists because `image-tiles-glob` gives one z plane per file (29 planes for an image with 29 masks) and
`png-channels` gives them as `c`, which a label may not have.

With `params.images: true` it reads the images those masks belong to, one PNG per plane (the **raw form**).
`image-tiles-glob` reads the same values; the raw form exists because both arrays of such a dataset come from
one zip on one host, and a host that limits requests per minute refuses the raw's requests as readily as the
label's (see "Requests and 429" below).

- **URLs.** Zip members only: `<zip url>::<member>` or a member glob `<zip url>::<pattern>` (`fnmatch` syntax;
  `*` also crosses `/`), expanded with `io.expand`. All urls of one array must name the same zip; a file named
  twice raises, and so does a plain (non-member) url.
- **Grouping (label form).** A member must be named `<image>_<k>.<ext>` with `k` a decimal number >= 1; anything
  else the glob matches raises (so a glob has to select the mask files only, e.g. `*/masks_png/*.png`). The
  masks of one image are the members with the same `<image>` (the whole member path before `_<k>`) and `<ext>`.
  Two members that are the same `k` of one image (`_1` and `_01`) raise.
- **Ids.** In a plane, a pixel is `k` where mask `k` is non-zero and 0 where no mask is. `k` comes from the file
  name, so ids are the dataset's own numbering and a missing `k` leaves a gap, not a shifted id.
- **Overlap.** Masks are painted in ascending `k`: where several masks are non-zero the pixel gets the
  **highest** `k`. The lower masks lose those pixels (and a mask wholly covered by later ones would vanish);
  the snippet does not report it. Measured on the example dataset below.
- **Order of planes.** Label form: images are sorted by `<image><ext>`, the name each image would have without
  `_<k>`. Raw form: the files are sorted by member name. Both whatever the order of the urls in the binding. So
  the two forms agree when the image files are named and laid out like the masks (same folders, sorted by
  member name). The snippet cannot check one array against the other; the binding's `reasons` must say why
  plane z of the label is the image at plane z of the raw. An image without any mask file has no plane in the
  label, which the harness reports as a shape mismatch with the raw.
- **Label form:** axes `zyx`, or `yx` when the urls hold the masks of one image only; dtype `uint16` always (a
  `k` above 65535 raises at open). Masks are grey PNGs (colour type 0, any bit depth); a palette, colour or
  alpha PNG raises: it has no single stored value to compare with 0. The masks of one image must have one
  shape, and every plane the shape of plane 0; one that differs raises at `read`.
- **Raw form** (`images: true`): one 8 or 16-bit grey or RGB PNG per plane, values as stored. Axes `zyx` for
  grey and `zcyx` for RGB (c = R, G, B), without `z` for a single file; dtype `uint8` or `uint16` from the
  first file. A palette, 1/2/4-bit or alpha PNG raises (the decoder would replace or rescale the stored
  values, and alpha is not a channel of the image); so does a file whose shape or dtype differs from the
  first. It takes no other param: no `samples: grey` as `image-tiles-glob` has, and no JPEG or TIFF.
- **Voxel size:** `None` in both forms.
- **Access.** `plane`: a plane is the whole image, or all the masks of one image, whatever the region asked
  for. `open_array` reads the zip directory and plane 0 (for the shape and dtype); the last plane read is kept.

## Requests and 429

Written after the first crop job on the example dataset ended with `429 Too Many Requests`, about ten requests in,
from a raw read through `image-tiles-glob`. Zenodo states a limit of 133 requests a minute
(`x-ratelimit-limit`, `retry-after: 60`) for an address, shared by everything that runs from it; the harness
retries dropped connections and 5xx answers but not a 429, and an error in a raw read ends a crop job.

- **One request per plane.** The zip is opened once per array with `io.open(<zip url>)` and read with
  `zipfile` (which inflates each member and checks its CRC), so reads go through `io`'s 1 MiB block cache.
  Before the members of a plane are read, the span from the first one's header to the last one's end is read
  once when it is at most 4 MiB, so `io` asks for the missing blocks in one request. `io.fetch` of a member is
  two requests (its 30-byte header, then its data): up to 244 for one label plane of the example.
- **A 429 is waited out.** Each step that makes requests (expanding a glob, opening the zip, reading a plane's
  span, reading a member) is tried up to 6 times while the answer is 429, waiting the answer's `Retry-After`
  seconds (60 when it has none, never less than 1 or more than 120) before each further try. Blocks a failed try
  did get stay cached. Any other error is raised at once. A job can therefore sit for up to 10 minutes on one
  step before the 429 is raised after all.

## Known limits

- Bytes are traded for requests: a plane of a few kB of masks costs at least one 1 MiB block, plus the zip
  directory once per job (6.6 MB for the example's zip).
- The zip must be readable by range, or cached whole. Members staged one by one with `io.fetch` are not used,
  and a server that ignores `Range` fails.
- One zip per array; nested zips and HTTP directories are not read.
- No param chooses the overlap rule or relabels ids. Ids are unique within a plane, not across planes.
- The polygons some datasets ship beside such masks (COCO JSON) are not read.
- Only a 429 is waited for, and only up to 6 tries; a host that stays over its limit longer still fails the job.

## Example

`examples/zenodo-10475962-in-cellulo-crystals-maskrcnn.yaml`: brightfield RGB images of insect cells with
intracellular protein crystals, one folder per image with `images_png/<image>.png` (534x534 RGB) and
`masks_png/<image>_<k>.png` (534x534 grey, values 0 and 255, one per crystal), in a 9.2 GB zip on Zenodo. Raw
(raw form) is `zcyx` (403, 3, 534, 534) uint8, label `zyx` (403, 534, 534): the 403 annotated images of
`target_G` and `target_H`, 12,106 masks, 6 to 122 per image, numbered 1..N without gaps in every folder (from the
zip directory). In each folder the zip holds the image PNG, then the 3.8 MB TIFF, then the masks (at most 55 kB
from the first to the last), so an image and its masks are two requests, not one.

Checked for the label form on eight planes (z 0, 89, 100, 180, 230, 297, 300, 402; 6 to 122 masks each): every
`k` of the folder is present in the plane, the non-zero pixels are exactly the union of the masks, and pixels
covered by more than one mask are 0.2% to 5.6% of the foreground (20 to 505 pixels per image), which is where
the highest `k` was kept. Plane 0 (39 masks) equals a merge of the same members staged one by one with
`io.fetch`. The raw form equals `image-tiles-glob` on z 0 and z 200. Opening both arrays took 5 requests (a
HEAD, two for the directory, one each for plane 0 of raw and label) and each further plane one request. With
the client made to answer 429 to the next two requests (`retry-after: 2`), a raw read returned after two waits
and a label read after one. A local `harness crop --n 12` made 12 crops in 14 s (27.5 MB).

The smoke run warns `label 0: dtype uint16 is not the record's ['uint8', 'unknown']`: the record describes the
single mask files, the snippet returns ids. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
