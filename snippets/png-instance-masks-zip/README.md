# png-instance-masks-zip

Reads instance labels stored as one binary mask PNG per object, `<image>_<k>.png`, inside a zip, and presents
them as one array of instance ids: one plane per image, the images stacked along z. It exists because
`image-tiles-glob` gives one z plane per file (29 planes for an image with 29 masks) and `png-channels` gives
them as `c`, which a label may not have.

- **URLs.** Zip members only: `<zip url>::<member>` or a member glob `<zip url>::<pattern>` (`fnmatch` syntax;
  `*` also crosses `/`), expanded with `io.expand`. All urls of one array must name the same zip; a file named
  twice raises, and so does a plain (non-member) url.
- **Grouping.** A member must be named `<image>_<k>.<ext>` with `k` a decimal number >= 1; anything else the
  glob matches raises (so a glob has to select the mask files only, e.g. `*/masks_png/*.png`). The masks of one
  image are the members with the same `<image>` (the whole member path before `_<k>`) and `<ext>`. Two members
  that are the same `k` of one image (`_1` and `_01`) raise.
- **Ids.** In a plane, a pixel is `k` where mask `k` is non-zero and 0 where no mask is. `k` comes from the file
  name, so ids are the dataset's own numbering and a missing `k` leaves a gap, not a shifted id.
- **Overlap.** Masks are painted in ascending `k`: where several masks are non-zero the pixel gets the
  **highest** `k`. The lower masks lose those pixels (and a mask wholly covered by later ones would vanish);
  the snippet does not report it. Measured on the example dataset below.
- **Order of planes.** Images are sorted by `<image><ext>`, the name each image would have without `_<k>`,
  whatever the order of the urls in the binding. So
  the planes follow the order in which `image-tiles-glob` stacks raw files named like the masks (same folder
  layout, sorted by member name). The snippet cannot check this against the raw array; the binding's `reasons`
  must say why plane z of the label is the image at plane z of the raw. An image without any mask file has no
  plane here, which the harness reports as a shape mismatch with the raw.
- **Axes.** `zyx`, or `yx` when the urls hold the masks of one image only. **dtype** `uint16` always (a `k`
  above 65535 raises at open). **Voxel size:** `None`.
- **Files.** Grey PNGs (colour type 0, any bit depth). A palette, colour or alpha PNG raises: it has no single
  stored value to compare with 0. Every mask must have the shape of the first; one that differs raises at
  `read`.
- **Access.** `plane`: a plane is all the masks of one image whatever the region asked for. The zip is opened
  once with `io.open(<zip url>)` and read with `zipfile` (which inflates each member and checks its CRC), so
  reads go through `io`'s 1 MiB block cache: a plane costs the blocks that hold its masks (one request when
  they are neighbours in the zip), not two requests per mask as `io.fetch` of each member would. `open_array`
  reads the zip directory and the first mask (for the shape); the last merged plane is kept.

## Known limits

- Bytes are traded for requests: a plane of a few kB of masks costs at least one 1 MiB block, plus the zip
  directory once per job (about 5 MiB for the example's 23,672 entries).
- The zip must be readable by range, or cached whole. Members staged one by one with `io.fetch` are not used,
  and a server that ignores `Range` fails.
- One zip per array; nested zips and HTTP directories are not read.
- No param chooses the overlap rule or relabels ids. Ids are unique within a plane, not across planes.
- The polygons some datasets ship beside such masks (COCO JSON) are not read.

## Example

`examples/zenodo-10475962-in-cellulo-crystals-maskrcnn.yaml`: brightfield RGB images of insect cells with
intracellular protein crystals, one folder per image with `images_png/<image>.png` (534x534 RGB) and
`masks_png/<image>_<k>.png` (534x534 grey, values 0 and 255, one per crystal), in a 9.2 GB zip on Zenodo. Raw
(`image-tiles-glob`) is `zcyx` (403, 3, 534, 534), label `zyx` (403, 534, 534): the 403 annotated images of
`target_G` and `target_H`, 12,107 masks, 6 to 122 per image, numbered 1..N without gaps in every folder (from the
zip directory).

Checked on eight planes (z 0, 89, 100, 180, 230, 297, 300, 402; 6 to 122 masks each): every `k` of the folder is
present in the plane, the non-zero pixels are exactly the union of the masks, and pixels covered by more than one
mask are 0.2% to 5.6% of the foreground (20 to 505 pixels per image), which is where the highest `k` was kept.
The plane with 6 masks equals a merge of the same members staged one by one with `io.fetch`. Each plane cost one
range request. Zenodo's response headers state a limit of 133 requests a minute, and it answered one request
with `429 Too Many Requests` while this was written (the harness does not retry a 429); reading each mask as its
own member would take up to 244 requests for one plane.

The smoke run warns `label 0: dtype uint16 is not the record's ['uint8', 'unknown']`: the record describes the
single mask files, the snippet returns ids. The example binding's `snippets_commit` is the commit before this
snippet's own (a binding can't name the commit that adds it); a binding written by the worker names a commit
that contains the snippet.
