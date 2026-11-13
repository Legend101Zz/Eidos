"""Frozen, mechanism-agnostic round 1; no card names or generator constants."""
from . import registry
from .spec import expand, recipe_id


def recipe(kind, family, steps=None, **params):
    return dict(schema="recipe-spec/1", task_type=kind, steps=steps if steps is not None else [{"op": "standardize"}],
                model=dict(family=family, params=params))


def portfolio_specs(kind, *, n=0, d=0):
    linear = "logistic" if kind == "classification" else "ridge"
    param = "C" if kind == "classification" else "alpha"
    views = [[{"op": "standardize"}], [{"op": "standardize"}, {"op": "shadow_screen"}],
             [{"op": "standardize"}, {"op": "pca", "auto_rank": True, "whiten": True}]]
    grids = []
    for view in views:
        grids.append(recipe(kind, linear, view, **{param: [.01, .1, 1., 10.]}))
        grids.append(recipe(kind, "knn", view, n_neighbors=[3, 5, 11, 21], weights=["uniform", "distance"]))
        grids.append(recipe(kind, "svm_rbf", view, C=[.1, 1., 3., 10., 30.], gamma=[.001, .01, .1, 1., "scale"]))
    if kind == "classification":
        grids.extend([recipe(kind, "lda"), recipe(kind, "qda", reg_param=[.01, .1, .5])])
        for view in views:
            grids.append(recipe(kind, "gmm_class", view, k=[1, 2, 3, 4, 5, 6], covariance_type=["full", "diag"]))
    else:
        for view in views:
            grids.append(recipe(kind, "kernel_ridge", view, alpha=[.01, .1, 1., 10.], gamma=[.001, .01, .1, 1.]))
    for family in ("extra_trees", "random_forest"):
        grids.append(recipe(kind, family, [], max_features=[.5, 1.], n_estimators=[64, 128], min_samples_leaf=[2, 4]))
    grids.append(recipe(kind, "hgb", [], max_iter=[150, 100], max_leaf_nodes=[31, 15], learning_rate=[.1, .05]))
    for view in views[:2]:
        grids.append(recipe(kind, "mlp", view, width=[32, 64, 128], depth=2, members=5, max_iter=300))
    for pre in ("per_example_center", "per_example_standardize"):
        for family in ("svm_rbf", "knn"):
            grids.append(recipe(kind, family, [{"op": pre}, {"op": "standardize"}]))
    expanded = expand(grids)
    if n > 3000:
        # A linear-margin kernel-class scout has bounded memory and avoids an
        # all-RBF timeout wiping out this class before the dense grid starts.
        expanded.insert(0, recipe(kind, "svm_linear", C=.1))
    # Guarantee representative coverage before spending the remainder on density.
    first, rest, seen = [], [], set()
    for s in expanded:
        cls = registry.hypothesis(s)
        if cls not in seen:
            first.append(s)
            seen.add(cls)
        else:
            rest.append(s)
    scouts = [s for s in rest if s["steps"] and s["steps"][0]["op"].startswith("per_example")]
    rest = [s for s in rest if s not in scouts]
    values = first + scouts + rest
    if n > 5000:
        # Protect cheap representatives and distinct model families before adding
        # duplicate settings. In particular, three forest settings cannot crowd
        # out a compact histogram model. This policy depends only on n, d, specs.
        unique = {}
        for s in values:
            if s["model"]["family"] == "mlp":
                s["model"]["params"].update(members=1, width=32, max_iter=100)
            unique.setdefault(recipe_id(s), s)
        pool = list(unique.values())
        positions = {recipe_id(s): i for i, s in enumerate(pool)}
        def cost(s):
            return registry.REGISTRY[s["model"]["family"]]["cost"](n, d), positions[recipe_id(s)]
        representatives, extras = [], []
        for cls in dict.fromkeys(registry.hypothesis(s) for s in pool):
            candidates = [s for s in pool if registry.hypothesis(s) == cls]
            families = {}
            for s in sorted(candidates, key=cost):
                families.setdefault(s["model"]["family"], s)
            chosen = list(families.values())[:3]
            for s in sorted(candidates, key=cost):
                if len(chosen) >= 3:
                    break
                if s not in chosen:
                    chosen.append(s)
            chosen.sort(key=cost)
            representatives.append(chosen[0])
            extras.extend(chosen[1:])
        scout_extras = [s for s in extras if s["steps"] and s["steps"][0]["op"].startswith("per_example")]
        values = representatives + scout_extras + sorted([s for s in extras if s not in scout_extras], key=cost)
    return values
