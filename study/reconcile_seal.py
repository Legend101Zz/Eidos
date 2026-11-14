"""Trusted profile seal preflight; prints only verification booleans and counts.

Private reconcilers can call verify_profile instead of pinning a Pilot 4 roster
and digest. This module never dispatches a model or starts a lab.
"""

import argparse
import io
import math

from study.private_card_registry import PROFILES
from study.private_seal import configured_seal


def verify_profile(profile="pilot-4"):
    seal = configured_seal(profile=profile)
    for card in PROFILES[profile].cards:
        manifest = seal.card(card)
        if manifest["seeds"] != [11, 23, 37]:
            raise ValueError("private profile confirmation seeds mismatch")
        result = seal.score(card, io.BytesIO(seal._read(card, "baseline_test.npz")))
        if manifest["metric"] == "rmse" and not math.isclose(
            manifest["min_reference_gain"], seal.cards[card][1] * result["baseline"],
            rel_tol=1e-12, abs_tol=0.0,
        ):
            raise ValueError("private RMSE relative bar failed verification")
    return {"verified": True, "cards": len(seal.cards), "relative_bars_verified": True}


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="pilot-4")
    print(json.dumps(verify_profile(parser.parse_args().profile), sort_keys=True))
