"""Model persistence — the artifact, and the schema it was fitted against.

Persisting the booster alone is not enough, and the reason is worth stating
because the failure it prevents is silent.

LightGBM consumes `category` dtypes positionally: a categorical column is passed
as the integer codes of its categories, not as the strings. So if `issuer` was
fitted with categories `[AXIS, HDFC, ICICI, SBI]` and a scoring frame builds
its own categories from whatever rows it happens to hold — say `[HDFC, SBI]` —
then HDFC scores as 0 where the model learned 1. Nothing raises. The columns
line up, the dtypes match, the prediction comes back well-formed and confidently
wrong, and the only symptom is a model that evaluated beautifully offline and
allocates badly in production.

So the schema travels with the model: exact column order, and the exact category
list per categorical column. `align` reindexes an incoming frame onto it.
Categories the model never saw become NaN, which LightGBM handles natively as a
missing value — the correct behaviour for an issuer that appeared after training.
"""

from __future__ import annotations

import pathlib
import pickle
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from yukti.intelligence.features import CATEGORICAL, Frame

# Rebuilt by `make train`; deliberately not committed. A stale model checked in
# would be worse than no model, because it would look current.
ARTIFACT_DIR = pathlib.Path(__file__).resolve().parents[3] / "artifacts"


class ModelUnavailable(RuntimeError):
    """No fitted artifact on disk. The caller must decide, not guess.

    Raised rather than silently returning an untrained model: an untrained
    booster still predicts, and predictions from one would flow into the
    allocator as if they meant something.
    """


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """The exact shape a model was fitted against."""

    columns: tuple[str, ...]
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, X: pd.DataFrame) -> FeatureSchema:
        cats = {
            col: tuple(str(v) for v in X[col].cat.categories)
            for col in X.columns
            if str(X[col].dtype) == "category"
        }
        return cls(columns=tuple(X.columns), categories=cats)

    def align(self, X: pd.DataFrame) -> pd.DataFrame:
        """Reindex a frame onto this schema, restoring the fitted categories."""
        missing = [c for c in self.columns if c not in X.columns]
        if missing:
            raise ValueError(
                f"scoring frame is missing column(s) the model was fitted on: {missing}"
            )
        out = X.loc[:, list(self.columns)].copy()
        for col, cats in self.categories.items():
            # Re-typed against the FITTED categories, not against the values
            # present in this batch. Values outside them become NaN.
            out[col] = pd.Categorical(out[col].astype(object).astype(str),
                                      categories=list(cats))
        return out


@dataclass(frozen=True, slots=True)
class Artifact:
    """A fitted model plus everything needed to reproduce and audit it."""

    model: Any
    schema: FeatureSchema
    kind: str
    trained_at: datetime
    n_rows: int
    seed: int
    metrics: dict[str, float] = field(default_factory=dict)

    def score(self, frame: Frame) -> Any:
        """Predict on a frame, aligning it to the fitted schema first."""
        aligned = Frame(
            X=self.schema.align(frame.X),
            treated=frame.treated, outcome=frame.outcome,
            archetype=frame.archetype, case_id=frame.case_id,
            amount_paise=frame.amount_paise, cost_paise=frame.cost_paise,
        )
        return self.model.predict(aligned)


def _path(kind: str, directory: pathlib.Path | None = None) -> pathlib.Path:
    return (directory or ARTIFACT_DIR) / f"{kind}.pkl"


def save(
    model: Any, frame: Frame, kind: str, seed: int,
    metrics: dict[str, float] | None = None,
    directory: pathlib.Path | None = None,
) -> pathlib.Path:
    """Persist a fitted model with the schema of the frame it was fitted on."""
    # Built from the TRAINING frame, which is the only frame whose categories
    # are authoritative. Deriving it at scoring time would defeat the purpose.
    schema = FeatureSchema.from_frame(frame.X)

    unencoded = [c for c in CATEGORICAL
                 if c in frame.X.columns and c not in schema.categories]
    if unencoded:
        raise ValueError(
            f"declared categorical column(s) were not encoded before fitting: "
            f"{unencoded} — the model would have seen them as opaque objects"
        )

    artifact = Artifact(
        model=model, schema=schema, kind=kind,
        trained_at=datetime.now(UTC), n_rows=len(frame), seed=seed,
        metrics=metrics or {},
    )
    directory = directory or ARTIFACT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(kind, directory)
    with path.open("wb") as fh:
        pickle.dump(artifact, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return path


def load(kind: str, directory: pathlib.Path | None = None) -> Artifact:
    path = _path(kind, directory)
    if not path.exists():
        raise ModelUnavailable(
            f"no fitted '{kind}' model at {path} — run `make train` first"
        )
    with path.open("rb") as fh:
        return pickle.load(fh)


def available(kind: str, directory: pathlib.Path | None = None) -> bool:
    return _path(kind, directory).exists()
