from mia_harness.schemas import ngff


def test_strict_example_validates_and_a_wrong_version_does_not():
    doc = {"ome": {"version": "0.6", "multiscales": [{"coordinateSystems": [{"name": "p", "axes": [
        {"name": "y", "type": "space", "unit": "nanometer"}, {"name": "x", "type": "space", "unit": "nanometer"}]}],
        "datasets": [{"path": "s0", "coordinateTransformations": [{"type": "scale", "scale": [1.0, 1.0],
        "input": {"path": "s0"}, "output": {"name": "p"}}]}]}]}}
    assert ngff.errors("image", doc) == []
    assert ngff.errors("image", {"ome": {**doc["ome"], "version": "0.5"}})
    assert len(ngff.SCHEMAS_COMMIT) == 40
