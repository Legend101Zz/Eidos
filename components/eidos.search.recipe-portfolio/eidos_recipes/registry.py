"""Finite operator vocabulary and frozen, task-independent complexity prior.

Costs and byte models are conservative planning estimates, never measured facts.
The estimate is superseded by the actual exported checkpoint byte count.
"""

CLASSES = {"linear", "screen", "lowrank", "generative", "neighbors", "kernel", "trees", "neural"}
# Each schema entry is (type/enum, lower, upper); lists are grid axes, not vectors.
BOOL = (bool, None, None)
POS = ((int, float), 1e-12, 1e9)
INT = (int, 1, 100000)
FRACTION = ((int, float), 0., 1.)
TRANSFORMS = {
    "standardize": {"with_mean": BOOL, "with_std": BOOL},
    "robust_scale": {}, "quantile_normal": {"n_quantiles": INT},
    "log1p_signed": {}, "tfidf": {}, "row_l2": {},
    "per_example_center": {}, "per_example_standardize": {},
    "variance_screen": {"threshold": ((int, float), 0., 1e9)},
    "corr_dedupe": {"threshold": FRACTION},
    "shadow_screen": {"n_estimators": (int, 1, 500)},
    "importance_topk": {"k": INT},
    "pca": {"n": INT, "var": ((int, float), .01, .999999), "auto_rank": BOOL, "whiten": BOOL},
    "onehot_lowcard": {"max_categories": (int, 2, 256)},
    "binarize": {"threshold": ((int, float), -1e9, 1e9)},
}
TREE = {"n_estimators": (int, 1, 500), "min_samples_leaf": INT,
        "max_depth": (int, 1, 64), "max_features": ((int, float), .01, 1.)}
MODELS = {
    "ridge": {"alpha": POS}, "logistic": {"C": POS}, "lda": {}, "qda": {"reg_param": FRACTION},
    "gmm_class": {"k": (int, 1, 6), "bic": BOOL, "covariance_type": (("full", "diag"), None, None)},
    "knn": {"n_neighbors": INT, "weights": (("uniform", "distance"), None, None)},
    "svm_rbf": {"C": POS, "gamma": ((int, float, str), None, None)},
    "svm_linear": {"C": POS},
    "kernel_ridge": {"alpha": POS, "gamma": POS}, "svr": {"C": POS, "gamma": POS, "epsilon": POS},
    "extra_trees": TREE, "random_forest": TREE,
    "hgb": {"max_iter": (int, 1, 500), "max_leaf_nodes": (int, 2, 255), "learning_rate": POS, "l2_regularization": ((int, float), 0., 1e9)},
    "mlp": {"width": (int, 1, 512), "depth": (int, 1, 4), "members": (int, 1, 10),
            "max_iter": (int, 1, 1000), "alpha": POS},
}
CLASSIFICATION = {"logistic", "lda", "qda", "gmm_class"}
REGRESSION = {"ridge", "kernel_ridge", "svr"}
FAMILY_CLASS = {"ridge": "linear", "logistic": "linear", "lda": "generative", "qda": "generative",
                "gmm_class": "generative", "knn": "neighbors", "svm_rbf": "kernel", "svm_linear": "kernel",
                "kernel_ridge": "kernel", "svr": "kernel", "extra_trees": "trees", "random_forest": "trees", "hgb": "trees", "mlp": "neural"}
ORDER = {"linear": 0, "generative": 1, "neighbors": 2, "kernel": 3, "trees": 4, "neural": 5}


def applicable(kind):
    return CLASSES if kind == "classification" else CLASSES - {"generative"}


def _entry(name, schema, transform):
    cls = "screen" if name in ("shadow_screen", "importance_topk", "corr_dedupe", "variance_screen") else "lowrank" if name == "pca" else FAMILY_CLASS.get(name, "linear")
    scale = 8 if name in ("mlp", "svm_rbf", "svr", "kernel_ridge") else 4 if name in ("extra_trees", "random_forest", "shadow_screen") else 1
    return dict(schema=schema, hypothesis_class=cls,
                task_types=["classification"] if name in CLASSIFICATION else ["regression"] if name in REGRESSION else ["classification", "regression"],
                cost=lambda n, d: scale * max(1., n * d / 500000.),
                complexity=ORDER.get(cls, 1), checkpoint_bytes=lambda n, d: int(4096 + scale * n * max(d, 1) * 16),
                determinism="All model randomness comes from --seed; fitted transforms see training rows only.",
                transform=transform)


REGISTRY = {name: _entry(name, schema, transform) for transform, vocabulary in ((True, TRANSFORMS), (False, MODELS)) for name, schema in vocabulary.items()}


def hypothesis(spec):
    names = {s["op"] for s in spec["steps"]}
    if names & {"shadow_screen", "importance_topk"}:
        return "screen"
    if "pca" in names:
        return "lowrank"
    return FAMILY_CLASS[spec["model"]["family"]]


def complexity(spec):
    model = spec["model"]
    params = model.get("params", {})
    base = ORDER[FAMILY_CLASS[model["family"]]] * 10000
    return base + len(spec["steps"]) * 100 + int(params.get("k", params.get("depth", 1))) * 10 + int(params.get("members", 1))


def estimated_bytes(spec, n, d):
    model, params = spec["model"]["family"], spec["model"].get("params", {})
    if model in ("extra_trees", "random_forest"):
        # Node arrays + values; assume worst-case binary tree, multi-output class cap 16.
        return int(4096 + params.get("n_estimators", 64) * 2 * n / params.get("min_samples_leaf", 4) * 256)
    return REGISTRY[model]["checkpoint_bytes"](n, d)
