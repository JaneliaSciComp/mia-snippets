# tiff-pyramid-level

Reads one page of a tiled, multi-page TIFF or BigTIFF at a plain URL: one level of a whole-slide image pyramid,
or of a mask pyramid written the same way. Exactly one URL per array. The binding names the page with `level`,
and a read fetches and decodes only the tiles that overlap the requested region, never a whole page.

Why it exists: a pyramidal slide (here ASAP's writer: page 0 the scan, each further page half the one before) is
shown by tifffile as one series holding page 0 only, and every other TIFF snippet in the catalog decodes a whole
page per read. Page 0 of the example slide is 218682x95310x3 (62.5 GB decoded, 5.76 GB of JPEG tiles), and its
mask is drawn at half that resolution, so the two can only be paired by choosing a level of each.

- **Access.** `chunk`. The file is opened with `io.open`; opening reads the header, the IFD chain up to the
  page, and that page's tile offset and byte count tables (the example's smoke, which opens both files and
  reads one patch of each, fetches 6 MiB). A read asks for the bytes of the overlapping tiles only. Tiles that lie within 1 MiB of each other in the file come by one read,
  so a 256x256 box costs one to four range requests. Nothing is fetched whole and there is no fallback to
  `io.fetch`: a server without Range support raises `NeedsFetch`.
- **Axes.** `yx` for one sample per pixel, `yxc` for several (RGB: tifffile's `YXS`), in the file's order.
- **Dtype and values.** The page's stored samples, decoded by tifffile/imagecodecs (JPEG with the page's
  JPEGTables, LZW, deflate, uncompressed, with any predictor). JPEG is lossy: values are what the decoder
  returns. A tile the file does not store (byte count 0; mask writers skip empty tiles, 9212 of the example
  mask's 20116 at level 0) reads as 0.
- **Voxel size.** From the page's own `XResolution`/`YResolution` with `ResolutionUnit` centimetre or inch, as
  ASAP and OpenSlide read a slide's pixel spacing. Each level carries its own values, so nothing is scaled by
  the snippet. `None` when the unit is absent or "none", or a resolution is missing or zero. A scanner or
  writer that leaves a print resolution there (72 or 96 dpi) would be reported as 353 or 265 µm pixels: check
  the value against the record.

## Params

- `level` (optional, default 0): 0-based index of the page (IFD) in file order. In a pyramid written level by
  level this is the pyramid level. To pair a label with a raw, name the level of each that has the same y/x
  shape, and say in `reasons` how the shapes and pixel sizes were confirmed (`harness describe <url>` shows page
  0 only; the sizes of the other pages follow from the downsampling, and a wrong pairing fails smoke with both
  shapes in the error).
- `class_id` (optional): for a class-id mask, present uint8 1 where the page equals this id and 0 elsewhere.
  Use it when the binding's label names one class of a map that holds several: `segmentation_type: binary`
  encodes `!= 0`, which on the stored map would include every other class. The file does not say what its ids
  mean; read a coarse level, compare it with the raw, and put what was measured in `reasons`. Fails at open on
  a page with several samples or a non-integer dtype.

## Known limits

- Tiled pages only, 2D, with interleaved samples. Stripped pages, separate sample planes and tiles with depth
  are refused at open.
- `level` counts pages, not resolutions. A file that also holds label or macro images as pages, or keeps its
  levels in SubIFDs (OME-TIFF pyramids), is not understood: the snippet presents whatever page the index names
  and does not check that it is a reduced copy of page 0.
- Photometric interpretation is not applied beyond the JPEG decoder's colour conversion: palette indices and
  min-is-white samples are returned as stored.
- Plain URLs only. A stored zip member would open through `io.open` as well, but that is untested.
- A read holds the region's bounding box in memory (the strided or reversed selection is taken from it), and
  the compressed bytes of one run of tiles, at most 64 MiB plus the last tile.
- Zenodo rate-limits anonymous clients, and the harness does not retry a 429: a probe that walked all 17 pages
  of both example files within a minute was refused partway. The example's smoke (5 s) and a 4-crop run (12 s,
  23 MB, 17 candidate boxes) passed, but a long run on the finest levels, where consecutive boxes share no
  1 MiB block and each costs a few requests, may need pacing by the worker.

## Example

`examples/zenodo-3375528-tissue-segmentation-wsi-sample.yaml`: one slide of the record's sample,
`tongue_hne_00.tif` (BigTIFF, 9 pages, 512x512 JPEG tiles, RGB), with its two-class mask
`tongue_hne_00_mask_cl2_sp0.5.tif` (BigTIFF, 8 pages, 512x512 LZW tiles).

| page | slide | nm/px | mask | nm/px |
|---|---|---|---|---|
| 0 | 218682x95310x3 | 243.1 | 109341x47655 | 486.2 |
| 1 | 109341x47655x3 | 486.2 | 54670x23827 | 972.4 |
| 2 | 54670x23827x3 | 972.4 | 27335x11913 | 1944.7 |
| ... | | | | |
| 7 | 1708x744x3 | 31116 | 854x372 | 62232 |
| 8 | 854x372x3 | 62232 | | |

Slide page k+1 and mask page k have the same shape for every k, so any such pair can be bound. The example binds
the finest one, slide `level: 1` with mask `level: 0` (yxc 109341x47655x3 and yx 109341x47655, 486.185 nm).

The mask holds 0, 3 and 6 (the whole of page 5: 45%, 27%, 27%). Compared with slide page 6 on the same grid, 0 is
where the slide is pure white (not scanned), 3 the scanned glass around the sections (mean RGB 239,239,239) and 6
the stained tissue (mean RGB 208,167,187). The label is bound with `class_id: 6` as a binary tissue mask. The
sibling `..._mask_cl6_sp0.5.tif` holds 0 to 6; its 5 and 6 are this mask's 6 and its 1 to 4 this mask's 3, which
fits the annotation groups of the slide's ASAP XML but is not documented, so it is not bound.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the commit
that adds it); a binding written by the worker names a commit that contains the snippet.
