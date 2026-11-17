# png-image

Reads one PNG file as one 2D array, decoded with `imagecodecs`. It was written for a set of whole-slide
overview images with one mask each, where every slide has its own size and the masks are 1-bit PNGs.

- **URLs.** Exactly one file per array: `<zip url>::<member>` (stored or deflated) or a plain URL. A glob is
  accepted only when it matches one file. Two files raise: images of different sizes cannot share an array (an
  `Array` has one shape and a reader does not pad), so a binding lists each image as its own `raw` entry and
  each mask as its own `labels` entry with `pairs_with` naming its image. For files of one shape, stacked along
  z, see `image-tiles-glob`.
- **Greyscale** (PNG colour type 0) is `yx`. 8- and 16-bit files are returned as decoded (`uint8`, `uint16`).
  **1-, 2- and 4-bit files are returned as their stored values**, `uint8` in 0..1, 0..3 or 0..15: libpng
  stretches them to 0..255 (stored × 255, × 85, × 17), and the snippet divides that back, raising if a decoded
  value is not a multiple of the factor. A 1-bit mask therefore reads as 0/1.
- **Colour** (RGB, RGBA, grey+alpha; 8 or 16 bits) is `cyx`, the samples in file order as the c axis, alpha
  included. With **`samples: grey`** a file whose samples are identical copies (a mask or grey image saved as
  RGB) is returned as one `yx` plane, sample 0; samples that differ raise at `read`, and a greyscale file
  passes through. Set it only when the record says the samples are copies.
- **Refused.** Palette PNGs (colour type 3: the decoder would return palette colours, not the stored indices),
  and a file with a `tRNS` chunk (the decoder adds an alpha sample the header does not announce; raises at
  `read`). A file that does not start with the PNG signature raises at open, whatever its name.
- **Open reads the header only.** `open_array` reads the first 33 bytes (signature and `IHDR`: width, height,
  bit depth, colour type). For a plain URL or a stored member that is one range read. For a deflated member
  `io.open` raises `NeedsFetch`, so the snippet opens the zip itself through `io.open(<zip url>)` and inflates
  just the start of the member; with `io`'s 1 MiB blocks that is one block per file, plus the zip's directory.
  Only a server without Range makes it stage the file at open.
- **Access.** `file`: `read` gets the whole file (`io.open` for a plain URL or stored member, `io.fetch` for a
  deflated one, which the dataset cache keeps) and decodes it whole whatever the region. The last two decoded
  files of the job are kept (a raw and its label), so the c planes and several crops of one image cost one
  decode, and memory does not grow with the number of arrays a binding lists. `read` returns a copy.
- **Rate limits.** A host that answers `429 Too Many Requests` at open or at `read` is waited on: the snippet
  sleeps for the response's `Retry-After` seconds (60 s when it is absent or a date, 120 s at most), makes the
  call again, and raises the 429 after five waits. The harness's own retries cover dropped connections and 5xx
  and leave a 4xx final, so without the wait one refused request ends the job. What `io` already holds
  (validators, a zip's directory, blocks, fetched members) is not asked for twice.
- **Voxel size.** `None`. A `pHYs` chunk is not read: it is a print density more often than a pixel size.

## Known limits

- Requests add up. Open costs one request per file (a 1 MiB block; small members of one zip share blocks), and
  reading a deflated member through `io.fetch` one for its local header and one per 8 MiB. zenodo.org limits
  them (its responses say `x-ratelimit-limit: 133`, `retry-after: 60`): a crop job on 17 pairs of the example
  dataset ended on a 429 after 30 MB, and opening all 584 pairs in one run was refused after about 110 range
  requests in 20 s. The example therefore binds 11 pairs (a cold crop job of 16 crops made 41 requests in
  15 s), and the snippet waits on a 429. The waits are bounded, so a binding with hundreds of entries can
  still fail, after minutes of waiting; the rest of the dataset binds the same way once the files are in the
  dataset cache.
- The wait on a 429 was tested on a fake `io` only (Retry-After as seconds, as a date, absent, too long; five
  waits, then the error; other errors raised at once): the real runs after the change met no 429.
- The 2- and 4-bit, 16-bit, alpha and stored-member paths were tested on synthetic files only; the example
  exercises 1-bit grey masks, RGB masks with identical samples (`samples: grey`) and 8-bit RGB images, all
  deflated zip members.
- A mask whose size differs from its image's is not cropped or resampled here; the harness refuses the pair,
  so leave it out of the binding. In the example dataset that is most of two cohorts: of the first 12 pairs by
  name, 10 CINJ and 7 TCGA masks are one row and/or column larger than their image, and 2 TCGA masks are twice
  the image's size. The CWRU (110 compared) and HUP (6 compared) pairs agree. How to bind the others (which
  row or column to drop) is a decision the files do not state.
- Interlaced PNGs decode, but like every PNG only whole.
