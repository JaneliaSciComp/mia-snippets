# tiff-channels-glob

Reads a set of multi-page channel TIFFs, one image per file with one page per channel, and presents it as one
array: each file's channels are `cyx` and the files are stacked along `z`. It was written for PerkinElmer/Akoya
QPTIFF component files (inForm unmixed components of tissue microarray cores). `tiff-zip-member-2` reads such a
file, but one file per array, and the glob snippets read single-page files only.

- **URLs.** Each URL is a plain file, a zip member `<zip url>::<member>`, a zip member glob `<zip url>::<pattern>`
  or a glob over an HTTP directory index `<dir url>/::<pattern>` (`fnmatch` syntax). A glob is expanded with
  `io.expand` and sorted by name; several URLs are concatenated in binding order, and a file named twice raises.
- **Files.** tifffile's `series[0]` must have axes `CYX` with one `YX` page per channel (a QPTIFF's 'Baseline'
  series, an OME or ImageJ channel stack), or `YX` for a single channel. The other series and levels of a file
  (a QPTIFF's RGB thumbnail, its reduced-resolution pages, macro and label images) are not read.
- **Axes.** `zcyx`: `z` is the file (position in the expanded URL list), `c` the file's page order. One file
  drops `z` (`cyx`); one channel per file drops `c` (`zyx`). `z` is a stack of independent 2D images, not depth.
- **One array is files of one shape.** `open_array` reads the first file's header only. Every other file is
  checked against it when `read` opens it: channel count, yx shape, dtype and pixel size must agree, or `read`
  raises a `ValueError` naming the file. A set whose files differ in shape is bound as one array per shape. When
  the file names do not tell the shapes apart, each array lists its files as plain URLs; finding the groups
  takes one header read per file (see the example).
- **Access.** `chunk` when the first file's channel pages are tiled: `read` fetches and decodes only the tiles
  the region touches, through `io.open`. `plane` when they are stripped (the page is decoded whole). `file` when
  the first file had to be staged with `io.fetch` (a deflated zip member, or a server without Range). The last
  opened file stays open, so the channels of one file are read from one parsed header.
- **dtype and values.** The file's, unchanged (float32 normalized counts for inForm components).
- **Voxel size.** For a QPTIFF (`tifffile.TiffFile.is_qpi`) y and x come from `YResolution`/`XResolution` and
  `ResolutionUnit` of the first channel page, which that format defines as the scanner's pixels per unit. For
  any other TIFF `None`, since the tags then describe print size. No z size.
- **Params.** None.

## Known limits

- Channel names are not compared between files, only the channel count. `c` means the same marker in every
  file only when the files share one page order; a QPTIFF names each channel in its page's XML `<Name>`, and
  the binding's `reasons` should say how that was checked.
- A file whose channels sit in one page (a contiguous "shaped" tifffile stack, planar RGB) raises, as does a
  file with a z or time axis.
- Only full resolution is read. A QPTIFF's reduced-resolution pages are not offered as pyramid levels.
- Over HTTP each file opened costs its header: the first 1 MiB block for the files of the example, since `io`
  reads in 1 MiB blocks. A crop then costs the blocks holding 1 to 4 tiles. Crops drawn from many files
  therefore cost about 2 to 5 MiB each.
- A tile with no stored data reads as zeros, as tifffile reads it.

## Example

`examples/s-biad3742-lattice-a-lung-mif.yaml`: LATTICe-A lung adenocarcinoma TMA cores on the BioImage Archive,
5,388 QPTIFFs in two panels (hallmarks, 10 channels; immuno-oncology, 8 channels), float32, LZW, tiled 512x512,
each with a thumbnail and a 2x-reduced copy.

The binding is **partial: 329 of the 5,388 files**. The cores are not one shape. Of the 269 hallmarks files
P_0001_C1 to P_0100_C2, 213 are 2408x2408, 41 are 2892x2896 and 15 are 3612x3616; of 60 immuno-oncology files
(every 45th by name), 52 are 2408x2408, 5 are 2892x2896 and 3 are 3012x3016. The sizes are scattered through
the patients with no pattern in the names, so a glob per panel fails at the first file of another size and the
binding lists each group's files instead, as six raw arrays. It binds the files whose headers were read while
the snippet was written. Covering the dataset needs the header of each remaining file (about 1 MiB per file
through `io`, about 5 GB in all) and the same grouping.

The example binding's `snippets_commit` is the commit before this snippet's own (a binding can't name the
commit that adds it); a binding written by the worker names a commit that contains the snippet.
