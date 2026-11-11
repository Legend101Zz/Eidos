"""Model-safe metadata for opaque prospective cards."""

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
