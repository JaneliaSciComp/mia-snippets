"""Binding schema (SPEC.md section 4)."""
import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArrayBinding(_Strict):
    snippet: str
    urls: list[str]
    params: dict


class LabelBinding(ArrayBinding):
    pairs_with: int = Field(ge=0)
    label_key: str
    segmentation_type: Literal["instance", "semantic", "binary"]


class Binding(_Strict):
    record: str
    record_commit: str
    snippets_commit: str
    raw: list[ArrayBinding] = Field(min_length=1)
    labels: list[LabelBinding]
    reasons: list[str]

    @model_validator(mode="after")
    def _pairs_in_range(self):
        for lb in self.labels:
            if lb.pairs_with >= len(self.raw):
                raise ValueError(f"pairs_with {lb.pairs_with} out of range for {len(self.raw)} raw")
        return self


def load_binding(path: Path) -> Binding:
    return Binding.model_validate(yaml.safe_load(Path(path).read_text()))


def binding_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
