"""Model-safe metadata for opaque prospective cards."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown — to be supplied by the trusted plane"


@dataclass(frozen=True)
class CardProfile:
    cards: tuple[str, ...]
    # None means that neither a metric nor a relative bar has been published.
    metadata: dict[str, tuple[str, float]] | None
    commitment_file: str
    seal_root_env: str

    def bars(self):
        metadata = self.metadata
        if metadata is None:
            supplied = json.loads(os.environ.get("EIDOS_CARD_PROFILE_METADATA", "{}"))
            metadata = {card: supplied[card] for card in self.cards if card in supplied}
            if set(metadata) != set(self.cards):
                raise ValueError(UNKNOWN)
        return validate_bars(metadata, self.cards)

    def commitment(self):
        import re

        value = Path(__file__).with_name(self.commitment_file).read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("profile commitment is pending; trusted publication required")
        return value


def validate_bars(metadata, cards):
    if set(metadata) != set(cards):
        raise ValueError("profile metadata roster mismatch")
    result = {}
    for card, value in metadata.items():
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("invalid public card metadata")
        metric, bar = value
        if (metric not in ("accuracy", "rmse") or type(bar) not in (int, float)
                or bar != (0.02 if metric == "accuracy" else 0.05)):
            raise ValueError("invalid public relative bar rule")
        result[card] = (metric, bar)
    return result

# Accuracy margins are absolute; RMSE margins are fractions of the evaluator baseline.
# The exact absolute RMSE thresholds stay in the private, committed manifests.
PRIVATE_CARD_BARS = {
    "N05": ("accuracy", 0.02),
    "N06": ("accuracy", 0.02),
    "N07": ("rmse", 0.05),
    "N08": ("rmse", 0.05),
}
PRIVATE_CLASSIFICATION = frozenset(
    card for card, (metric, _) in PRIVATE_CARD_BARS.items() if metric == "accuracy"
)
PRIVATE_REGRESSION = frozenset(PRIVATE_CARD_BARS) - PRIVATE_CLASSIFICATION

PROFILES = {
    "pilot-4": CardProfile(tuple(PRIVATE_CARD_BARS), PRIVATE_CARD_BARS,
                           "pilot4_commitment.txt", "EIDOS_PRIVATE_SEAL_ROOT"),
    "pilot-5": CardProfile(tuple(f"N{i:02}" for i in range(9, 13)), None,
                           "pilot5_commitment.txt", "EIDOS_PILOT5_SEAL_ROOT"),
    "pilot-6": CardProfile(tuple(f"N{i:02}" for i in range(13, 17)), None,
                           "pilot6_commitment.txt", "EIDOS_PILOT6_SEAL_ROOT"),
}
ALL_PRIVATE_CARDS = frozenset(card for profile in PROFILES.values() for card in profile.cards)
SEAL_ROOT_ENVS = tuple(profile.seal_root_env for profile in PROFILES.values())


def profile_for_card(card):
    for name, profile in PROFILES.items():
        if card in profile.cards:
            return name
    raise ValueError("unsupported private card")


def card_bars(card):
    return PROFILES[profile_for_card(card)].bars()[card]


def is_classification(card):
    return card in ALL_PRIVATE_CARDS and card_bars(card)[0] == "accuracy"


def is_regression(card):
    return card in ALL_PRIVATE_CARDS and card_bars(card)[0] == "rmse"
