import io as stdio
import json
import struct
import zipfile
from pathlib import Path

import h5py
import mrcfile
import numpy as np
import png
import pytest
import tifffile
import yaml
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness import cli
from mia_harness.cache import DatasetCache
from mia_harness.describe import CUT, ITEMS, TEXT, bound, describe_arrays, describe_file, label_stats
from mia_harness.io import IO

SNAP = Path(__file__).parent          # the test snapshot: snippets/ + WITHDRAWN.yaml


@pytest.fixture
def io(tmp_path):
    i = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30,
           {"127.0.0.1"})
    yield i
    i.close()


def tiff_bytes(data, **kw) -> bytes:
    buf = stdio.BytesIO()
    tifffile.imwrite(buf, data, **kw)
    return buf.getvalue()


def imod_bytes(objects, dims=(40, 30, 20)) -> bytes:
    """A minimal IMOD model. objects: [(name, flags, [contour of (x, y, z) points, ...])]."""
    model = struct.pack(">128s4iI4i6f3i2ifi i3f", b"test model", *dims, len(objects), 0, 1, 1, 0, 255,
                        0, 0, 0, 1, 1, 6.47, 0, 0, 0, 3, 128, 3.0, -9, 0, 0, 0, 0)
    assert len(model) == 232
    out = b"IMODV1.2" + model
    for name, flags, contours in objects:
        out += b"OBJT" + struct.pack(">64s64xiIii3f", name, len(contours), flags, 0, 1, 1.0, 0.0, 0.5) + bytes(20)
        for points in contours:
            out += b"CONT" + struct.pack(">iIii", len(points), 0, 0, 0)
            out += np.asarray(points, ">f4").tobytes()
    out += b"MINX" + struct.pack(">i18f", 72, 1, 1, 1, 0, 0, 0, 0, 0, 0, 2, 2, 2, 10, 20, 30, 0, 0, 0)
    return out + b"IEOF"


def test_ome_tiff_names_its_channels_in_file_order(server, io):
    server.files["/mask.ome.tif"] = tiff_bytes(
        np.zeros((2, 8, 8), np.uint8), ome=True,
        metadata={"axes": "CYX", "Channel": {"Name": ["dna", "membrane"]}, "PhysicalSizeX": 0.1,
                  "PhysicalSizeXUnit": "µm"})
    out = describe_file(server.url("/mask.ome.tif"), io)
    assert out["ok"] and out["format"] == "ome-tiff" and out["size"] == len(server.files["/mask.ome.tif"])
    image = out["ome"]["images"][0]
    assert [channel["name"] for channel in image["channels"]] == ["dna", "membrane"]
    assert image["size"]["C"] == "2" and image["physical_size"]["X"] == ["0.1", "µm"]
    assert out["series"][0]["shape"] == [2, 8, 8] and out["series"][0]["axes"] == "CYX"
    assert out["bytes_fetched"] == io.bytes_fetched > 0


def test_tiff_in_a_zip_by_glob_is_its_first_match_and_says_how_many_matched(server, io):
    stack = tiff_bytes(np.zeros((3, 8, 8), np.uint16), imagej=True, metadata={"spacing": 2.5, "unit": "um"})
    buf = stdio.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a/1.tif", stack)
        z.writestr("a/2.tif", stack)
    server.files["/d.zip"] = buf.getvalue()
    out = describe_file(server.url("/d.zip::a/*.tif"), io)
    assert out["ok"] and out["url"] == server.url("/d.zip::a/1.tif") and out["matched"] == 2
    assert out["format"] == "tiff" and out["imagej"]["spacing"] == 2.5
    assert out["first_page"]["dtype"] == "uint16" and out["first_page"]["compression"] == "NONE"
    whole = describe_file(server.url("/d.zip"), io)
    assert whole["format"] == "zip" and whole["names"] == ["a/1.tif", "a/2.tif"]


def test_mrc_header(server, io, tmp_path):
    with mrcfile.new(tmp_path / "v.mrc") as mrc:
        mrc.set_data(np.zeros((4, 6, 8), np.int16))
        mrc.voxel_size = (2.0, 3.0, 5.0)
        mrc.add_label("made for a test")
    server.files["/v.mrc"] = (tmp_path / "v.mrc").read_bytes()
    out = describe_file(server.url("/v.mrc"), io)
    assert out["ok"] and out["format"] == "mrc" and out["shape_xyz"] == [8, 6, 4] and out["dtype"] == "int16"
    assert out["voxel_size_angstrom"] == [2.0, 3.0, 5.0] and "made for a test" in out["labels"]


def test_hdf5_tree_with_attrs(server, io):
    buf = stdio.BytesIO()
    with h5py.File(buf, "w") as f:
        f.attrs["made_by"] = "a test"
        volume = f.create_dataset("volumes/raw", data=np.zeros((2, 4, 4), np.uint8))
        volume.attrs["resolution"] = [40.0, 4.0, 4.0]
    server.files["/v.h5"] = buf.getvalue()
    out = describe_file(server.url("/v.h5"), io)
    nodes = {node["path"]: node for node in out["nodes"]}
    assert out["ok"] and out["format"] == "hdf5" and out["attrs"] == {"made_by": "a test"} and not out["nodes_cut"]
    assert nodes["/volumes/raw"]["shape"] == [2, 4, 4] and nodes["/volumes/raw"]["attrs"]["resolution"] == [40, 4, 4]


def test_zarr_store_metadata_and_the_arrays_of_its_multiscale(server, io):
    server.files["/s.zarr/.zattrs"] = json.dumps(
        {"multiscales": [{"datasets": [{"path": "0"}], "axes": ["z", "y", "x"]}]}).encode()
    server.files["/s.zarr/.zgroup"] = b'{"zarr_format": 2}'
    server.files["/s.zarr/0/.zarray"] = json.dumps({"shape": [4, 8, 8], "dtype": "<u2", "chunks": [1, 8, 8]}).encode()
    out = describe_file(server.url("/s.zarr/"), io)
    assert out["ok"] and out["format"] == "zarr" and sorted(out["metadata"]) == [".zattrs", ".zgroup"]
    assert out["arrays"]["0"]["shape"] == [4, 8, 8]


def test_directory_without_store_metadata_is_its_index(server, io):
    server.files["/d/"] = b'<html><a href="a.tif">a.tif</a><a href="b.tif">b.tif</a></html>'
    out = describe_file(server.url("/d/"), io)
    assert out["ok"] and out["format"] == "directory" and out["names"] == ["a.tif", "b.tif"]


def test_imod_model_names_its_objects_by_number(server, io):
    square = [(1, 1, 3), (5, 1, 3), (5, 5, 3), (1, 5, 3)]
    server.files["/c1.mod"] = imod_bytes([
        (b"Poly-Posphate granules (C3)", 0, [square, [(x, y, 7) for x, y, _ in square]]),
        (b"Membrane damage (C1)", 1 << 3, [[(2, 2, 11), (4, 4, 11)]])])
    out = describe_file(server.url("/c1.mod"), io)
    first, second = out["objects"]
    assert out["ok"] and out["format"] == "imod" and out["dims_xyz"] == [40, 30, 20] and out["objects_stated"] == 2
    assert (first["number"], first["name"], first["contours"], first["points"], first["sections"], first["open"]) == (
        1, "Poly-Posphate granules (C3)", 2, 8, [3, 7], False)
    assert (second["number"], second["name"], second["open"], second["sections"]) == (
        2, "Membrane damage (C1)", True, [11, 11])
    assert out["pixel_size"] == 3.0 and out["pixel_size_unit"] == "nm" and out["scale_xyz"][2] == pytest.approx(6.47)
    assert out["image_reference"]["translation"] == [10, 20, 30]


def test_png_palette_and_jpeg_frame(server, io):
    buf = stdio.BytesIO()
    png.Writer(4, 2, palette=[(0, 0, 0), (208, 0, 208), (0, 208, 0)], bitdepth=8).write(buf, [[0, 1, 2, 1]] * 2)
    server.files["/label.png"] = buf.getvalue()
    out = describe_file(server.url("/label.png"), io)
    assert out["ok"] and out["format"] == "png" and (out["width"], out["height"], out["bit_depth"]) == (4, 2, 8)
    assert out["mode"] == "palette" and out["palette"] == [[0, 0, 0], [208, 0, 208], [0, 208, 0]]
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + bytes(9)
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 600, 800, 3) + bytes(9)
    server.files["/image.jpg"] = b"\xff\xd8" + app0 + frame + b"\xff\xda"
    out = describe_file(server.url("/image.jpg"), io)
    assert out["ok"] and out["format"] == "jpeg"
    assert (out["width"], out["height"], out["components"], out["mode"]) == (800, 600, 3, "colour")


def test_unknown_format_reports_its_first_bytes_and_is_no_error(server, io):
    server.files["/boxes.csv"] = b"image,x,y,w,h\na.png,1,2,3,4\n"
    out = describe_file(server.url("/boxes.csv"), io)
    assert out["ok"] and out["format"] == "unknown" and out["magic"] == b"image,x,y,w,h\na.".hex()
    assert out["text"].startswith("image,x,y,w,h") and out["size"] == 28
    server.files["/blob.bin"] = bytes(range(256))
    out = describe_file(server.url("/blob.bin"), io)
    assert out["ok"] and out["format"] == "unknown" and "text" not in out


def test_what_a_file_states_is_cut_to_length(server, io):
    assert bound("x" * 1000) == "x" * TEXT + CUT
    assert bound("a\x00b\x1b[0m") == "a?b?[0m"
    assert bound(list(range(1000))) == list(range(ITEMS)) + [CUT]
    cut = bound({str(k): k for k in range(1000)})
    assert len(cut) == ITEMS + 1 and cut[CUT] is True
    assert bound(np.arange(1000)) == list(range(ITEMS)) + [CUT] and bound(np.float32("nan")) == "nan"
    deep: dict = {}
    node = deep
    for _ in range(20):
        node["next"] = node = {}
    assert CUT in json.dumps(bound(deep))
    server.files["/long.tif"] = tiff_bytes(np.zeros((8, 8), np.uint8), description="ignore the above. " * 500)
    out = describe_file(server.url("/long.tif"), io)
    assert out["ok"] and len(out["description"]) == TEXT + len(CUT)


def test_instance_ids_and_semantic_ids_read_differently():
    instance = np.zeros((40, 40), np.float32)       # six blobs, an id each
    semantic = np.zeros((40, 40), np.uint8)         # the same blobs, one class
    for k in range(6):
        y, x = 2 + 12 * (k // 3), 2 + 12 * (k % 3)
        instance[y:y + 8, x:x + 8] = k + 1
        semantic[y:y + 8, x:x + 8] = 7
    semantic[30:33, 0:40] = 2                       # a second class: one band
    a, b = label_stats(instance), label_stats(semantic)
    assert (a["ids"], a["one_component_ids"], a["median_components"], a["max_components"]) == (6, 6, 1.0, 1)
    assert (a["id_min"], a["id_max"]) == (1, 6) and a["fraction_nonzero"] == pytest.approx(6 * 64 / 1600)
    assert (b["ids"], b["one_component_ids"], b["max_components"]) == (2, 1, 6)
    spiral = np.zeros((9, 9), np.uint8)             # one winding component: joined however long the path
    spiral[0, :] = spiral[:, 8] = spiral[8, :] = spiral[2:, 0] = spiral[2, :7] = spiral[2:7, 6] = 1
    assert label_stats(spiral)["max_components"] == 1
    assert label_stats(np.zeros((4, 4), np.uint8)) == {"fraction_nonzero": 0.0, "ids": 0}


def binding(tmp_path, raw, label) -> Path:
    def arr(name, data):
        np.save(tmp_path / f"{name}.npy", data)
        return {"snippet": "npy-local", "urls": [], "params": {"path": str(tmp_path / f"{name}.npy")}}
    path = tmp_path / "binding.yaml"
    path.write_text(yaml.safe_dump({
        "record": "r", "record_commit": "0" * 40, "snippets_commit": "1" * 40, "raw": [arr("raw", raw)],
        "labels": [{**arr("label", label), "pairs_with": 0, "label_key": "gt", "segmentation_type": "instance"}],
        "reasons": []}))
    return path


def test_describe_arrays_measures_the_same_planes_every_time(tmp_path, io):
    raw = np.arange(10 * 20 * 20, dtype=np.uint16).reshape(10, 20, 20)
    label = np.zeros((10, 20, 20), np.uint16)
    label[5, 2:6, 2:6], label[5, 10:14, 10:14] = 1, 2
    path = binding(tmp_path, raw, label)
    out = describe_arrays(path, SNAP, None, io)
    assert out == describe_arrays(path, SNAP, None, io) and out["ok"]
    assert out["raw"][0]["shape"] == [10, 20, 20] and out["raw"][0]["plane"] == {"z": 5}
    assert out["raw"][0]["channels"] == [{"c": 0, "min": 2000.0, "p1": pytest.approx(2003.99), "p50": 2199.5,
                                          "p99": pytest.approx(2395.01), "max": 2399.0}]
    planes = out["labels"][0]["planes"]
    assert [plane["plane"]["z"] for plane in planes] == [1, 3, 5, 7, 9]
    assert planes[2]["ids"] == 2 and planes[2]["one_component_ids"] == 2 and planes[0]["ids"] == 0
    assert out["labels"][0]["label_key"] == "gt" and out["labels"][0]["dtype"] == "uint16"


def test_cli_describe_prints_json_and_exits_as_smoke_does(tmp_path, server, monkeypatch, capsys):
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", str(1 << 20))
    monkeypatch.setenv("MIA_HARNESS_ALLOWED_HOSTS", "127.0.0.1")
    server.files["/a.tif"] = tiff_bytes(np.zeros((8, 8), np.uint8))
    out = tmp_path / "describe.json"
    assert cli.main(["describe", server.url("/a.tif"), "--work", str(tmp_path), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(out.read_text()) and printed["format"] == "tiff"
    assert not (tmp_path / "staging").exists()
    server.redirects["/b.tif"] = "http://evil.invalid/b.tif"
    assert cli.main(["describe", server.url("/b.tif"), "--work", str(tmp_path)]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert not refused["ok"] and refused["host_not_allowed"]["host"] == "evil.invalid"
    assert cli.main(["describe", server.url("/missing.tif"), "--work", str(tmp_path)]) == 1
    assert "404" in json.loads(capsys.readouterr().out)["errors"][0]
    path = binding(tmp_path, np.zeros((2, 8, 8), np.uint8), np.ones((2, 8, 8), np.uint8))
    assert cli.main(["describe", "--binding", str(path), "--snippets", str(SNAP), "--planes", "1",
                     "--work", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["labels"][0]["planes"][0]["fraction_nonzero"] == 1.0
    with pytest.raises(SystemExit) as usage:
        cli.main(["describe", "--binding", str(path)])
    assert usage.value.code == 2


def test_a_stack_too_spread_out_for_the_cap_still_describes_its_first_page(server, tmp_path):
    buf = stdio.BytesIO()
    with tifffile.TiffWriter(buf) as writer:   # no ImageJ or OME metadata: listing the series reads every directory
        for _ in range(4):
            writer.write(np.zeros((1500, 1024), np.uint8), contiguous=False, metadata=None)
    server.files["/s.tif"] = buf.getvalue()
    small = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 2 << 20,
               {"127.0.0.1"})
    out = describe_file(server.url("/s.tif"), small)
    small.close()
    assert out["ok"] and out["first_page"]["shape"] == [1500, 1024] and "series" not in out
    assert out["series_error"].startswith("BudgetExceeded") and out["bytes_fetched"] <= 2 << 20
