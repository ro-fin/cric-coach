"""Canonical vocabulary shared by every producer and consumer (US-B4, US-G1).

These enums are the contract language of the whole system. Values are stable
identifiers: renaming one is a schema-major change (see US-G1 versioning).
"""

from enum import StrEnum


class Line(StrEnum):
    """Delivery line channels for a right-hand batter (mirrored for LH, US-C5)."""

    OUTSIDE_OFF = "outside_off"
    OFF = "off"
    MIDDLE = "middle"
    LEG = "leg"


class Length(StrEnum):
    YORKER = "yorker"
    FULL = "full"
    GOOD = "good"
    SHORT = "short"


class Shot(StrEnum):
    """Granular shot labels; the 8-way decision classifier (US-E4) uses DECISION_SHOTS."""

    LEAVE = "leave"
    DEFEND = "defend"
    DRIVE = "drive"
    COVER_DRIVE = "cover_drive"
    STRAIGHT_DRIVE = "straight_drive"
    ON_DRIVE = "on_drive"
    CUT = "cut"
    PULL = "pull"
    HOOK = "hook"
    SWEEP = "sweep"
    FLICK = "flick"
    LOFT = "loft"


#: The 8 canonical decision classes from US-E4 ("front/back foot, leave, defend,
#: drive, cut, pull, sweep, loft"); granular drives collapse into DRIVE.
DECISION_SHOTS: frozenset[Shot] = frozenset(
    {
        Shot.LEAVE,
        Shot.DEFEND,
        Shot.DRIVE,
        Shot.CUT,
        Shot.PULL,
        Shot.SWEEP,
        Shot.FLICK,
        Shot.LOFT,
    }
)

#: Collapse granular shots to their 8-way decision class.
SHOT_TO_DECISION: dict[Shot, Shot] = {
    Shot.COVER_DRIVE: Shot.DRIVE,
    Shot.STRAIGHT_DRIVE: Shot.DRIVE,
    Shot.ON_DRIVE: Shot.DRIVE,
    Shot.HOOK: Shot.PULL,
}


def decision_class(shot: Shot) -> Shot:
    """Map any granular shot to its 8-way decision class (US-E4)."""
    return SHOT_TO_DECISION.get(shot, shot)


class Footwork(StrEnum):
    FRONT = "front"
    BACK = "back"
    LEAVE = "leave"


class Contact(StrEnum):
    MIDDLE = "middle"
    EDGE = "edge"
    MISS = "miss"


class Outcome(StrEnum):
    """Per-ball outcome labels (US-B4/US-G1 example: 'controlled_ground_shot')."""

    CONTROLLED_GROUND_SHOT = "controlled_ground_shot"
    CONTROLLED_AERIAL = "controlled_aerial"
    UNCONTROLLED = "uncontrolled"
    BEATEN = "beaten"
    BOWLED = "bowled"
    EDGED = "edged"
    LEFT_ALONE = "left_alone"


class BowlerSource(StrEnum):
    """Who delivered the ball (US-A3, US-B3)."""

    MACHINE = "machine"
    HUMAN = "human"
    COACH = "coach"


class SessionType(StrEnum):
    BATTING = "batting"
    BOWLING = "bowling"
    MIXED = "mixed"


class BlockIntent(StrEnum):
    """Practice-block intent for the batting quality split (US-B3, US-H2)."""

    TECHNICAL = "technical"
    DECISION = "decision"
    MATCH_SCENARIO = "match_scenario"
    SPIN_SPECIFIC = "spin_specific"
    FUN = "fun"


class Handedness(StrEnum):
    RIGHT = "right"
    LEFT = "left"


class Provenance(StrEnum):
    """How a metric value was produced (US-G1: provenance on every field)."""

    MANUAL = "manual"
    AUTO = "auto"
    PROXY = "proxy"


class VideoStatus(StrEnum):
    """Per-file upload/probe status (US-B2)."""

    PENDING = "pending"
    UPLOADED = "uploaded"
    PROBED = "probed"
    FAILED = "failed"
    METADATA_CONFLICT = "metadata_conflict"


#: Video rows that prove usable footage exists for a camera. FAILED rows had
#: their bytes deleted (checksum mismatch) and must never count as evidence.
#: Canonical for both the API capture-state seam and the worker clip cutter.
EVIDENCE_STATUSES: frozenset[VideoStatus] = frozenset(
    {VideoStatus.UPLOADED, VideoStatus.PROBED, VideoStatus.METADATA_CONFLICT}
)


class CalibrationKind(StrEnum):
    """What a calibration record parameterizes (US-C1/C2, US-F6)."""

    INTRINSIC = "intrinsic"
    EXTRINSIC = "extrinsic"
    STEREO = "stereo"  # extrinsics between a camera pair (US-F6 triangulation)


class EventSource(StrEnum):
    """How a ball event entered the dataset (US-D1/D4)."""

    AUTO = "auto"
    MANUAL = "manual"
    CORRECTED = "corrected"


class ClipStatus(StrEnum):
    """Per-ball per-camera clip state (US-D2). GAP records a missing camera loudly."""

    PENDING = "pending"
    CUT = "cut"
    FAILED = "failed"
    GAP = "gap"


class AnchorPoint(StrEnum):
    """Frame-lock anchor for clip comparison (US-E5)."""

    RELEASE = "release"
    CONTACT = "contact"


class MetricPhase(StrEnum):
    """Which slice of the ball a metric set describes (US-E2/E3/E4)."""

    PRE_RELEASE = "pre_release"
    CONTACT = "contact"
    FLIGHT = "flight"


class Role(StrEnum):
    """Access roles (US-L3): Parent (admin), Coach (review), Player (age-appropriate)."""

    PARENT = "parent"
    COACH = "coach"
    PLAYER = "player"


class DatasetSplit(StrEnum):
    """Membership split inside a dataset version (US-F1).

    The test split is session-disjoint from train/val — enforced at freeze time.
    """

    TRAIN = "train"
    VAL = "val"
    TEST = "test"


class LabelClass(StrEnum):
    """Detector label classes (US-F1); glove/helmet are optional extras."""

    BALL = "ball"
    BAT = "bat"
    STUMPS = "stumps"
    FEET = "feet"
    GLOVE = "glove"
    HELMET = "helmet"


class AnnotationSource(StrEnum):
    """How an annotation entered the dataset (US-F1 provenance)."""

    MANUAL = "manual"
    IMPORTED = "imported"  # round-tripped from an external labeling tool
    MODEL = "model"  # pre-labeled by a model, pending human review


class ModelStage(StrEnum):
    """Registry promotion stages (US-F2): candidate -> staging -> production."""

    CANDIDATE = "candidate"
    STAGING = "staging"
    PRODUCTION = "production"


class TrainingStatus(StrEnum):
    """Lifecycle of one training/eval run (US-F2)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DeliveryIntensity(StrEnum):
    """How taxing a delivered ball is on a young body (US-H1 bowling ledger)."""

    SPIN = "spin"
    PACE_INTENT = "pace_intent"
    THROWDOWN = "throwdown"


class ReportKind(StrEnum):
    """Report cadence (US-G3/G5): one ``reports`` table, ``kind`` discriminates."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ReportStatus(StrEnum):
    """Report lifecycle (US-G3). Publishing is gated by the safety validator
    (US-H5): a report violating H1/H4 state lands in BLOCKED, never PUBLISHED."""

    DRAFT = "draft"
    PUBLISHED = "published"
    BLOCKED = "blocked"


class StageStatus(StrEnum):
    """Pipeline run and per-stage lifecycle (US-J1, US-L1)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AlertAudience(StrEnum):
    """Who an alert is routed to (US-L4): parents get honesty/quality banners,
    developers get drift and canary failures."""

    PARENT = "parent"
    DEVELOPER = "developer"


class EvidenceVerdict(StrEnum):
    """Coach's call on whether the linked clips support a finding (US-G6)."""

    CONFIRMS = "confirms"
    NOT_SUPPORTED = "not_supported"


class SafetyCode(StrEnum):
    """Workload & safety violation codes (US-H1/H4/H5).

    Not a column type: codes travel inside SafetyVerdict JSON payloads, but the
    vocabulary is canonical here so every producer and validator agrees.
    """

    WORKLOAD_CEILING = "workload_ceiling"
    DAY_PATTERN_VIOLATION = "day_pattern_violation"
    PAIN_FLAG = "pain_flag"


class FindingSeverity(StrEnum):
    """Severity ladder for coaching findings (US-G3 ranking, US-J2).

    Not a column type: ``findings.severity`` stores these values as plain
    strings so the findings contract stays JSON-portable across agents.
    """

    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"


class CameraRole(StrEnum):
    """What a registered camera looks at (US-I1). ``camera_configs.role`` is
    registry metadata plus the ``GET /cameras?role=`` listing filter; no
    pipeline stage consumes cameras by role today — stages select by camera id
    (e.g. bowling-action reads C5). Role-driven stage consumption is a future
    seam. C5-C7 register as bowling-side roles; existing rows stay NULL until
    re-registered."""

    BATTING_SIDE = "batting_side"
    BOWLING_SIDE = "bowling_side"
    WRIST = "wrist"
    FRONT_ON = "front_on"
    OTHER = "other"


class BowlingVariation(StrEnum):
    """Leg-spin delivery variations (US-I6). ``delivery_labels.variation_intent``
    is the bowler/coach ground truth; ``variation_detected`` is the classifier's
    honest V1 prediction (never conflated). ``unknown`` is a first-class value —
    an unclassifiable delivery is labeled, never dropped."""

    LEG_BREAK = "leg_break"
    TOP_SPINNER = "top_spinner"
    GOOGLY = "googly"
    SLIDER = "slider"
    FLIPPER = "flipper"
    UNKNOWN = "unknown"


class MilestoneKind(StrEnum):
    """What a logged milestone celebrates (US-K4/G5). ``milestones.kind``
    discriminates one append-only log: a personal best on a metric, a bowling
    volume landmark, or a practice streak."""

    PERSONAL_BEST = "personal_best"
    VOLUME = "volume"
    STREAK = "streak"


class BraceState(StrEnum):
    """Front-leg brace quality at release (US-I2 action checkpoint).

    Not a column type: brace state travels inside BallRecord v1.1 bowling
    fields and ``ball_metrics`` payloads, but the vocabulary is canonical here
    so every producer (release evaluator) and consumer (report) agrees.
    """

    BRACED = "braced"
    BENT = "bent"
    COLLAPSED = "collapsed"


class ReviewMode(StrEnum):
    """Report review-gate mode (US-J5). Not a column type: the value lives
    inside ``app_settings.settings['report_review']['mode']`` JSON, but the
    vocabulary is canonical here so the generate/publish gate and the settings
    API agree. ``coach_gate`` holds a generated report in draft with a
    ``review_due_at`` deadline; ``auto_publish`` publishes immediately."""

    AUTO_PUBLISH = "auto_publish"
    COACH_GATE = "coach_gate"
