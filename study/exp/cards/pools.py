"""Family-constrained public/private assignment and sealed pool assembly."""

import argparse
import json
import os
import random
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from .common import CARD_FILES, inventory, read_json, sha, write_json
from .qualify import verify_card
from .select import family, row_band

DEFAULT_SIZES = {"D": 8, "E1": 8, "E2": 8, "E3": 8, "H": 12}


def _public_card(card, pool):
    fields = ("card_id", "task_type", "n_rows", "n_features", "row_band", "imbalanced")
    result = {name: card[name] for name in fields if name in card}
    if pool == "D":
        result.update(source=card.get("source"), key=card.get("key"))
    return result


def assign(catalog, sizes=None, seed=20261114, private_seed_file=None, *, allow_shortfall=False):
    if private_seed_file is None:
        raise ValueError("private_seed_file is required")
    sizes = dict(DEFAULT_SIZES if sizes is None else sizes)
    if any(type(size) is not int or size < 0 for size in sizes.values()) or "H" not in sizes:
        raise ValueError("pool sizes must be nonnegative integers including H")
    private_seed_file = Path(private_seed_file)
    private_seed_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not private_seed_file.exists():
        fd = os.open(private_seed_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(os.urandom(32))
    hidden_seed = private_seed_file.read_bytes()
    if len(hidden_seed) != 32:
        raise ValueError("private seed must contain exactly 32 bytes")
    cards = catalog["cards"] if isinstance(catalog, dict) else catalog
    if len({c["card_id"] for c in cards}) != len(cards):
        raise ValueError("duplicate card IDs")
    groups = defaultdict(list)
    for card in cards:
        groups[card.get("family", family(card.get("key", card["card_id"])))].append(card)
    rng = random.Random(seed)
    group_list = [groups[key] for key in sorted(groups)]
    rng.shuffle(group_list)
    assignments = {}
    # Reserve capacity for H before lower-priority pools using ONLY the public RNG.
    # H's actual draw then uses its private seed from the reservation plus unused reserve.
    # This preserves D/E determinism while giving shortages the D,E1,H,E2,E3 priority.
    reserved_h = []
    order = [p for p in ("D", "E1", "H", "E2", "E3") if p in sizes]
    order.extend(p for p in sizes if p not in order)
    for pool in order:
        if pool == "H":
            if allow_shortfall:
                members, group_list = _draw(group_list, sizes[pool], rng, max_null=null_limit(pool, sizes[pool]),
                                            allow_shortfall=True)
                reserved = defaultdict(list)
                for card in members:
                    reserved[card.get("family", family(card.get("key", card["card_id"])))].append(card)
                reserved_h = list(reserved.values())
            continue
        assignments[pool], group_list = _draw(group_list, sizes[pool], rng, max_null=null_limit(pool, sizes[pool]),
                                               allow_shortfall=allow_shortfall)
    group_list += reserved_h
    hidden_rng = random.Random(int.from_bytes(hidden_seed, "big"))
    hidden_rng.shuffle(group_list)
    assignments["H"], reserve = _draw(group_list, sizes["H"], hidden_rng, max_null=null_limit("H", sizes["H"]),
                                       allow_shortfall=allow_shortfall)
    public = dict(seed=seed, sizes={p: n for p, n in sizes.items() if p != "H"},
                  pools={p: [_public_card(c, p) for c in assignments[p]] for p in sizes if p != "H"})
    private = dict(pools=assignments, reserve=[c for group in reserve for c in group])
    write_json(private_seed_file.parent / "assignments.json", private)
    os.chmod(private_seed_file.parent / "assignments.json", 0o600)
    return public


def null_limit(pool, size):
    return min(size, 2 if pool == "H" else max(1, size // 8))


def _draw(groups, size, rng, *, max_null=None, allow_shortfall=False):
    """Subset-sum over size AND null count, preserving whole families."""
    if max_null is None:
        max_null = size
    states = {(0, 0): [()]}
    for i, group in enumerate(groups):
        nulls = sum(bool(c.get("null_card", False)) for c in group)
        for (count, old_nulls), subsets in sorted(list(states.items()), reverse=True):
            new_count, new_nulls = count + len(group), old_nulls + nulls
            if new_count > size or new_nulls > max_null:
                continue
            key = new_count, new_nulls
            states.setdefault(key, []).extend(indices + (i,) for indices in list(subsets))
            if len(states[key]) > 200:
                states[key].sort(key=lambda indices: _balance([c for j in indices for c in groups[j]], new_count))
                states[key] = states[key][:200]
    feasible = max(count for count, _ in states)
    if feasible != size and not allow_shortfall:
        raise ValueError("not enough cards or family/null constraints cannot fill a pool exactly")
    candidates = [indices for (count, _), subsets in states.items() if count == feasible for indices in subsets]
    rng.shuffle(candidates)
    selected = min(candidates, key=lambda indices: _balance([c for j in indices for c in groups[j]], feasible))
    return [c for i in selected for c in groups[i]], [group for i, group in enumerate(groups) if i not in selected]


def _balance(cards, size):
    counts = Counter((c["task_type"], c.get("row_band", row_band(c["n_rows"]))) for c in cards)
    return sum((counts[kind, band] - size / 6) ** 2 for kind in ("classification", "regression") for band in range(3))


def build_pool(pool_id, cards, pool_root):
    root = Path(pool_root)
    if root.exists() and any(root.iterdir()):
        raise ValueError("pool output must be new or empty")
    prepared, excluded = [], []
    for card_dir in cards:
        card = Path(card_dir)
        manifest = verify_card(card)
        controller = card.parent.parent / "controller" / manifest["id"]
        qualification = read_json(controller / "qualification.json")
        if manifest["qualified"] is not True or qualification["qualified"] is not True:
            excluded.append(manifest["id"])
            continue
        prepared.append((card, controller, manifest))
    if len({m["id"] for _, _, m in prepared}) != len(prepared):
        raise ValueError("duplicate card IDs")
    nulls = sum(bool(read_json(controller / "reference.json")["null_card"]) for _, controller, _ in prepared)
    if nulls > null_limit(pool_id, len(prepared)):
        raise ValueError("pool exceeds null-card allowance")
    root.mkdir(parents=True, exist_ok=True)
    (root / "seal").mkdir()
    (root / "controller").mkdir()
    metadata = []
    for card, controller, manifest in prepared:
        card_id = manifest["id"]
        shutil.copytree(card, root / "seal" / card_id)
        shutil.copytree(controller, root / "controller" / card_id)
        task = read_json(card / "bundle/task.json")
        reference = read_json(controller / "reference.json")
        metadata.append(dict(card_id=card_id, task_type=manifest["task_type"], metric=manifest["metric"],
                             bar_rule="absolute accuracy gain >= 0.02" if manifest["metric"] == "accuracy" else
                                      "absolute RMSE reduction >= 0.05 * seed-11 baseline test RMSE",
                             min_reference_gain=manifest["min_reference_gain"],
                             n_train=manifest["splits"]["train"]["n"],
                             n_features=task["inputs"]["train"]["shape"][1], null_card=reference["null_card"]))
    files = inventory(root / "seal")
    salt = os.urandom(32)
    digest = sha(salt + json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
    write_json(root / "seal/commitment-opening.json", dict(files=files, salt=salt.hex(), sha256=digest))
    result = dict(pool_id=pool_id, card_ids=[c["card_id"] for c in metadata], cards=metadata, commitment=digest)
    write_json(root / "pool.json", result)
    verify(root)
    return dict(**result, excluded_unqualified=excluded)


def verify(pool_root, commitment=None):
    root = Path(pool_root)
    seal = root / "seal"
    if seal.is_symlink():
        raise ValueError("symlink in seal")
    opening_path = seal / "commitment-opening.json"
    if opening_path.is_symlink():
        raise ValueError("symlink in seal")
    opening = read_json(opening_path)
    hashes, salt = opening["files"], bytes.fromhex(opening["salt"])
    public = read_json(root / "pool.json")
    expected = public["commitment"] if commitment is None else commitment
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    if len(salt) != 32 or sha(salt + canonical) != expected or opening["sha256"] != expected:
        raise ValueError("seal commitment mismatch")
    for name, digest in hashes.items():
        relative = PurePosixPath(name)
        if (name != relative.as_posix() or relative.is_absolute() or ".." in relative.parts or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("invalid sealed path or digest")
    required = {f"{card}/{name}" for card in public["card_ids"] for name in CARD_FILES | {"manifest.json"}}
    if not required <= hashes.keys():
        raise ValueError("seal omits required files")
    if inventory(seal, {"commitment-opening.json"}) != hashes:
        raise ValueError("seal file digest/inventory mismatch")
    for card in public["card_ids"]:
        manifest = verify_card(seal / card)
        if manifest["qualified"] is not True:
            raise ValueError("pool contains unqualified card")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assignment = commands.add_parser("assign")
    assignment.add_argument("--catalog", required=True)
    assignment.add_argument("--seed", type=int, default=20261114)
    assignment.add_argument("--private-seed-file", required=True)
    assignment.add_argument("--sizes", help='JSON object, e.g. {"D":8,"E1":8,"E2":8,"E3":8,"H":12}')
    assignment.add_argument("--out", required=True)
    build = commands.add_parser("build")
    build.add_argument("--pool-id", required=True)
    build.add_argument("--cards", nargs="+", required=True)
    build.add_argument("--pool-root", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--pool-root", required=True)
    check.add_argument("--commitment")
    args = parser.parse_args()
    if args.command == "assign":
        result = assign(read_json(args.catalog), json.loads(args.sizes) if args.sizes else None,
                        args.seed, args.private_seed_file)
        write_json(args.out, result)
        print("public D/E assignment written; H and reserve saved only beside the private seed")
    elif args.command == "build":
        result = build_pool(args.pool_id, args.cards, args.pool_root)
        print(f"built {result['pool_id']}: {len(result['card_ids'])} cards; excluded {result['excluded_unqualified']}")
    else:
        print(verify(args.pool_root, args.commitment))


if __name__ == "__main__":
    main()
