"""TEST ONLY: imports a forbidden module."""
import subprocess  # noqa: F401  (the point of this fixture)

SPEC_VERSION = 1


def open_array(src, io):
    raise NotImplementedError
