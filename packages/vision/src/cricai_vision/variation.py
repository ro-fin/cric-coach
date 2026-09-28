"""Leg-spin variation classification spine (US-I6): features, protocol, honest eval.

Honest V1: the classifier consumes GEOMETRIC outcome features measured from the
tracked trajectory and release primitives (post-bounce turn, flight apex, dip,
release height) — never an unmeasurable wrist claim. The 3-way vocabulary is
``leg_break`` / ``top_spinner`` / ``googly``; slider/flipper are deferred (their
intents are excluded from the 3-way eval, marked experimental). A low-confidence
delivery is labeled ``unknown`` — unclear, never force-classified (US-I6 AC).

All classification code depends on :class:`VariationClassifierProtocol`, never a
concrete model, so tests and the worker's offline default run on the seeded
deterministic :class:`FakeVariationClassifier`; a trained model slots in behind
the same protocol once it clears the T3 gate (:func:`meets_agreement_gate`,
>= 80% 3-way intent agreement) — below the gate its labels never appear anywhere
kid-facing.

Sign convention (cross-agent contract, US-I5 producer): ``turn_cm`` is the
post-bounce lateral deviation in centimeters, positive when the ball turns away
from a right-handed batter — the leg-break direction for a right-arm wrist
spinner; negative is the googly direction.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from cricai_data.enums import BowlingVariation

#: The honest V1 3-way vocabulary (US-I6; slider/flipper deferred).
VARIATION_CLASSES: tuple[str, ...] = (
    BowlingVariation.LEG_BREAK.value,
    BowlingVariation.TOP_SPINNER.value,
    BowlingVariation.GOOGLY.value,
)

#: The "unclear" label: an unclassifiable delivery is labeled, never dropped.
UNCLEAR_LABEL: str = BowlingVariation.UNKNOWN.value

#: Pinned classifier headline metric keys (0-1 fractions, the models-API scale):
#: 3-way intent agreement and the honest unclear rate.
CLASSIFIER_HEADLINE_KEYS: tuple[str, ...] = ("variation_agreement_3way", "unclear_rate")

#: Fraction-valued per-class row keys of the classifier eval artifact.
CLASSIFIER_CLASS_FRACTION_KEYS: tuple[str, ...] = ("precision", "recall")

#: Report detail keys the classifier artifact carries beyond the pinned base.
CLASSIFIER_REPORT_EXTRA_KEYS: tuple[str, ...] = ("confusion_matrix",)

#: T3 gate: >= 80% 3-way agreement before auto labels appear anywhere kid-facing.
AGREEMENT_GATE = 0.80


class VariationError(ValueError):
    """Invalid variation-classification input (features, config, eval pairs)."""


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise VariationError(f"{name} must be a finite number, got {value!r}")


@dataclass(frozen=True)
class DeliveryFeatures:
    """Geometric outcome features of one delivery (US-I6 classifier input).

    ``turn_cm`` is required (a delivery with no measured post-bounce deviation
    cannot be classified); the flight/release features are optional and only
    sharpen confidence when present. All values follow the module's sign
    convention and are validated here — a corrupt stored value fails loudly
    instead of silently steering a prediction.
    """

    turn_cm: float
    apex_m: float | None = None
    dip_flag: bool | None = None
    release_height_cm: float | None = None

    def __post_init__(self) -> None:
        _require_finite("turn_cm", self.turn_cm)
        if self.apex_m is not None:
            _require_finite("apex_m", self.apex_m)
            if self.apex_m <= 0:
                raise VariationError(f"apex_m must be > 0, got {self.apex_m!r}")
        if self.release_height_cm is not None:
            _require_finite("release_height_cm", self.release_height_cm)
            if self.release_height_cm <= 0:
                raise VariationError(
                    f"release_height_cm must be > 0, got {self.release_height_cm!r}"
                )


@dataclass(frozen=True)
class VariationPrediction:
    """One delivery's prediction: a 3-way class or ``unknown``, with confidence."""

    variation: BowlingVariation
    confidence: float

    def __post_init__(self) -> None:
        if self.variation.value not in VARIATION_CLASSES and self.variation.value != UNCLEAR_LABEL:
            raise VariationError(
                f"prediction vocabulary is {VARIATION_CLASSES} + {UNCLEAR_LABEL!r}, "
                f"got {self.variation.value!r} (slider/flipper are deferred, US-I6)"
            )
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise VariationError(f"confidence must be within [0, 1], got {self.confidence!r}")


class VariationClassifierProtocol(Protocol):
    """Classifies one delivery's geometric features into the 3-way vocabulary."""

    @property
    def version(self) -> str:
        """Identifier recorded as provenance (model_runs trainer_version, summaries)."""
        ...

    def classify(self, features: DeliveryFeatures) -> VariationPrediction:
        """Predict the variation; deterministic for fixed features."""
        ...


@dataclass(frozen=True)
class FakeVariationClassifier:
    """Deterministic geometric-rule classifier — tests and the offline default.

    A pure function of the features (no randomness): big turn away from the
    batter reads as a leg break, big turn in as a googly, a straight-on
    delivery with measured dip as a top spinner, and everything in between as
    ``unknown``. Confidence grows with the turn margin and any prediction under
    ``confidence_floor`` collapses to ``unknown`` — unclear is a first-class
    honest answer, never a forced class (US-I6 AC).
    """

    big_turn_cm: float = 8.0
    straight_turn_cm: float = 3.0
    confidence_floor: float = 0.5
    version: str = "fake-variation-1"

    def __post_init__(self) -> None:
        if self.big_turn_cm <= 0:
            raise VariationError(f"big_turn_cm must be > 0, got {self.big_turn_cm!r}")
        if not 0 < self.straight_turn_cm < self.big_turn_cm:
            raise VariationError(
                f"straight_turn_cm must be within (0, big_turn_cm), got {self.straight_turn_cm!r}"
            )
        if not 0.0 <= self.confidence_floor <= 1.0:
            raise VariationError(
                f"confidence_floor must be within [0, 1], got {self.confidence_floor!r}"
            )

    def classify(self, features: DeliveryFeatures) -> VariationPrediction:
        turn = features.turn_cm
        if abs(turn) >= self.big_turn_cm:
            variation = BowlingVariation.LEG_BREAK if turn > 0 else BowlingVariation.GOOGLY
            confidence = min(1.0, abs(turn) / (2 * self.big_turn_cm))
        elif abs(turn) <= self.straight_turn_cm and features.dip_flag:
            variation = BowlingVariation.TOP_SPINNER
            # A measured apex corroborates the flight shape; without it the
            # dip evidence stands alone and the confidence stays lower.
            confidence = 0.75 if features.apex_m is not None else 0.6
        else:
            return VariationPrediction(BowlingVariation.UNKNOWN, 0.0)
        if confidence < self.confidence_floor:
            return VariationPrediction(BowlingVariation.UNKNOWN, 0.0)
        return VariationPrediction(variation, round(confidence, 4))


# --- Honest eval (US-I6: agreement/confusion shown honestly) --------------------


@dataclass(frozen=True)
class VariationEval:
    """3-way intent-vs-detected evaluation of one labeled set (US-I6).

    ``matrix[intent][detected]`` counts pairs whose INTENT is in the 3-way
    vocabulary; detections outside it (``unknown``, deferred classes) fold into
    the ``unknown`` column — the matrix never invents a column. Pairs whose
    intent is outside the 3-way (slider/flipper/unknown, deferred) are excluded
    from the matrix but counted in ``excluded_intents`` so nothing disappears
    silently. ``agreement_3way``/``unclear_rate`` are ``None`` — never a
    fabricated number — when their denominator is empty.
    """

    matrix: dict[str, dict[str, int]]
    excluded_intents: int
    n_pairs: int
    n_classified: int
    agreement_3way: float | None
    unclear_rate: float | None

    def headline_metrics(self) -> dict[str, float]:
        """The pinned :data:`CLASSIFIER_HEADLINE_KEYS` metrics (0-1 fractions).

        Raises :class:`VariationError` when there is no evaluable pair — a
        model run must never record a fabricated headline.
        """
        if self.agreement_3way is None or self.unclear_rate is None:
            raise VariationError(
                "no evaluable intent/detected pairs; refusing to fabricate headline metrics"
            )
        return {
            "variation_agreement_3way": round(self.agreement_3way, 4),
            "unclear_rate": round(self.unclear_rate, 4),
        }

    def per_class_rows(self) -> list[dict[str, Any]]:
        """Per-class precision/recall/support rows for the eval artifact.

        A zero denominator reads as 0.0 — absence of evidence never reads as
        quality (conservative, matching the honesty rules).
        """
        rows: list[dict[str, Any]] = []
        for label in VARIATION_CLASSES:
            support = sum(self.matrix[label].values())
            predicted = sum(self.matrix[intent][label] for intent in VARIATION_CLASSES)
            correct = self.matrix[label][label]
            rows.append(
                {
                    "label": label,
                    "precision": round(correct / predicted, 4) if predicted else 0.0,
                    "recall": round(correct / support, 4) if support else 0.0,
                    "support": support,
                }
            )
        return rows

    def to_report(self) -> dict[str, Any]:
        """Trainer-report detail for :func:`cricai_vision.train.build_eval_report`
        (``per_class`` rows + the ``confusion_matrix`` extra key)."""
        return {
            "per_class": self.per_class_rows(),
            "confusion_matrix": {intent: dict(row) for intent, row in self.matrix.items()},
        }


def evaluate_variations(
    pairs: Sequence[tuple[BowlingVariation, BowlingVariation]],
) -> VariationEval:
    """Evaluate (intent, detected) pairs into the honest 3-way stats (US-I6).

    ``intent`` is the human ground truth, ``detected`` the classifier output —
    the caller must never conflate them (contract #5). Deliveries the classifier
    has not labeled (NULL ``variation_detected``) are not predictions and must
    not be passed in.
    """
    matrix: dict[str, dict[str, int]] = {
        intent: dict.fromkeys((*VARIATION_CLASSES, UNCLEAR_LABEL), 0)
        for intent in VARIATION_CLASSES
    }
    excluded = 0
    for intent, detected in pairs:
        if intent.value not in matrix:
            excluded += 1  # deferred/unknown intent: outside the 3-way eval
            continue
        column = detected.value if detected.value in VARIATION_CLASSES else UNCLEAR_LABEL
        matrix[intent.value][column] += 1
    n_pairs = sum(sum(row.values()) for row in matrix.values())
    n_classified = sum(
        matrix[intent][detected] for intent in VARIATION_CLASSES for detected in VARIATION_CLASSES
    )
    correct = sum(matrix[label][label] for label in VARIATION_CLASSES)
    return VariationEval(
        matrix=matrix,
        excluded_intents=excluded,
        n_pairs=n_pairs,
        n_classified=n_classified,
        agreement_3way=correct / n_classified if n_classified else None,
        unclear_rate=1.0 - n_classified / n_pairs if n_pairs else None,
    )


def meets_agreement_gate(agreement: float | None, *, gate: float = AGREEMENT_GATE) -> bool:
    """T3 gate predicate: >= 80% 3-way agreement (US-I6 AC).

    ``None`` (no evaluable evidence) never passes — below or without the gate
    the auto label appears nowhere kid-facing.
    """
    if not 0.0 < gate <= 1.0:
        raise VariationError(f"gate must be within (0, 1], got {gate!r}")
    return agreement is not None and agreement >= gate
