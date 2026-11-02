"""Validators for the vendored OME-Zarr 0.6 JSON schemas (ome/ngff-spec tag 0.6)."""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS_DIR = Path(__file__).parent / "ngff-0.6"
SCHEMAS_COMMIT: str = (SCHEMAS_DIR / "COMMIT").read_text().split()[1]


@lru_cache(maxsize=1)
def _registry() -> Registry:
    """A registry resolving each schema's `$id` URL (and so every cross-file `$ref`) to its vendored file."""
    resources = []
    for path in sorted(SCHEMAS_DIR.glob("*.schema")):
        contents = json.loads(path.read_text())
        resources.append((contents["$id"], Resource.from_contents(contents, default_specification=DRAFT202012)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    """The validator for the vendored schema `<name>.schema`."""
    schema = json.loads((SCHEMAS_DIR / f"{name}.schema").read_text())
    return Draft202012Validator(schema, registry=_registry())


def errors(name: str, document: object) -> list[str]:
    """Every violation of schema `name` by `document`, each as `"<json pointer>: <message>"`, sorted."""
    return sorted(f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(name).iter_errors(document))
