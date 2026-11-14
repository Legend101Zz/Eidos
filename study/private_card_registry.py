"""Model-safe metadata for opaque prospective cards."""

import json
import os
import re
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

    def seal_root(self):
        return Path(os.environ[self.seal_root_env])

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
# Controller locations must never reach lab/arm processes.
SEAL_ROOT_ENVS = tuple(profile.seal_root_env for profile in PROFILES.values()) + (
    "EIDOS_POOLS_ROOT", "EIDOS_EXTRA_POOL_MANIFESTS", "EIDOS_IMP_PRIVATE_ROOT",
)


@dataclass(frozen=True)
class PoolProfile:
    pool: str
    cards: tuple[str, ...]
    metadata: dict[str, tuple[str, float]]
    digest: str

    def bars(self):
        return validate_bars(self.metadata, self.cards)

    def commitment(self):
        return self.digest

    def pool_root(self):
        for root in os.environ.get("EIDOS_POOLS_ROOT", "").split(os.pathsep):
            if root and (Path(root) / self.pool / "seal").is_dir():
                return Path(root) / self.pool
        raise ValueError("pool seal is not configured")

    def seal_root(self):
        return self.pool_root() / "seal"

    def public_manifest(self):
        # Safe worker roster, without manifest paths, source names or dimensions.
        return dict(pool=self.pool, commitment=self.digest, cards=[
            dict(id=card, metric=metric, task_type="classification" if metric == "accuracy" else "regression",
                 bar_rule=pool_bar_rule(metric)) for card, (metric, _) in self.metadata.items()])


def pool_bar_rule(metric):
    return ("absolute accuracy gain >= 0.02" if metric == "accuracy" else
            "absolute RMSE reduction >= 0.05 * seed-11 baseline test RMSE")


def load_pool_manifest(data, profiles):
    pool = data.get("pool", data.get("pool_id"))
    digest = data.get("commitment")
    if (not isinstance(pool, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", pool)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("invalid public pool identity or commitment")
    metadata = {}
    legacy = {f"D{i:02}" for i in range(1, 13)} | {f"N{i:02}" for i in range(1, 5)}
    occupied = legacy | {card for profile in profiles.values() for card in profile.cards}
    for item in data["cards"]:
        card = item.get("id", item.get("card_id"))
        metric = item["metric"]
        if (not isinstance(card, str) or not re.fullmatch(r"R[0-9]{4}", card)
                or card in occupied or card in metadata):
            raise ValueError("pool card id collision or invalid id")
        if metric not in ("accuracy", "rmse") or item["task_type"] != (
                "classification" if metric == "accuracy" else "regression"):
            raise ValueError("invalid public pool metric/task type")
        if item["bar_rule"] != pool_bar_rule(metric):
            raise ValueError("invalid public pool bar rule")
        metadata[card] = (metric, 0.02 if metric == "accuracy" else 0.05)
    if not metadata or "pool:" + pool in profiles:
        raise ValueError("empty or duplicate pool profile")
    profiles["pool:" + pool] = PoolProfile(pool, tuple(metadata), metadata, digest)


def load_pools(profiles, directory, extra=(), worker=()):
    for path in [*sorted(Path(directory).glob("*.json")), *map(Path, extra)]:
        data = json.loads(path.read_text())
        # legacy-references.json is a reference map, not a pool manifest.
        if "pool" in data or "pool_id" in data:
            load_pool_manifest(data, profiles)
    for data in worker:
        name = "pool:" + data["pool"]
        if name in profiles:
            if profiles[name].public_manifest() != data:
                raise ValueError("worker pool metadata differs from public manifest")
        else:
            load_pool_manifest(data, profiles)


load_pools(PROFILES, Path(__file__).resolve().parents[1] / "experiments/pools",
           filter(None, os.environ.get("EIDOS_EXTRA_POOL_MANIFESTS", "").split(os.pathsep)),
           json.loads(os.environ.get("EIDOS_STUDY_POOL_PROFILES", "[]")))
ALL_PRIVATE_CARDS = frozenset(card for profile in PROFILES.values() for card in profile.cards)


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
