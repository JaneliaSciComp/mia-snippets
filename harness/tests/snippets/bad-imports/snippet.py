"""TEST ONLY: imports a forbidden module."""
import subprocess  # noqa: F401  (the point of this fixture)

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1


def open_array(src: Source, io: IO) -> Array:
    raise NotImplementedError
