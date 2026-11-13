"""Hand-written operator validation after the manifest's coarse acceptance check."""
import copy
import itertools
import math

from .registry import MODELS, REGISTRY, TRANSFORMS
from .util import canonical

MAX_RECIPES = 400


def _params(name, params, schema, grid):
    if not isinstance(params, dict):
        raise ValueError(name + " parameters must be an object")
    for key, value in params.items():
        if key not in schema:
            raise ValueError(name + ": unknown parameter " + key)
        values = value if grid and isinstance(value, list) else [value]
        if not values:
            raise ValueError(name + "." + key + ": empty grid axis")
        kind, low, high = schema[key]
        for item in values:
            if isinstance(kind, tuple) and kind and isinstance(kind[0], str):
                valid = item in kind
            else:
                valid = isinstance(item, kind) and (kind is bool or not isinstance(item, bool))
            if not valid or isinstance(item, float) and not math.isfinite(item):
                raise ValueError(name + "." + key + ": invalid value")
            if isinstance(item, (int, float)) and kind is not bool and (low is not None and item < low or high is not None and item > high):
                raise ValueError(name + "." + key + ": outside allowed range")
            if key == "gamma" and isinstance(item, str) and item not in ("scale", "auto"):
                raise ValueError(name + ".gamma: use scale, auto or a positive number")
            if key == "gamma" and isinstance(item, (int, float)) and item <= 0:
                raise ValueError(name + ".gamma: must be positive")


def validate(spec, *, grid=False):
    if not isinstance(spec, dict) or set(spec) != {"schema", "task_type", "steps", "model"}:
        raise ValueError("recipe needs schema, task_type, steps and model only")
    if spec["schema"] != "recipe-spec/1" or spec["task_type"] not in ("classification", "regression"):
        raise ValueError("invalid recipe schema or task_type")
    if not isinstance(spec["steps"], list) or len(spec["steps"]) > 16:
        raise ValueError("steps must be a list of at most 16 operators")
    for step in spec["steps"]:
        if not isinstance(step, dict) or step.get("op") not in TRANSFORMS:
            raise ValueError("unknown operator " + str(step.get("op") if isinstance(step, dict) else step))
        name = step["op"]
        _params(name, {k: v for k, v in step.items() if k != "op"}, TRANSFORMS[name], grid)
        if name == "pca" and len(set(step) & {"n", "var", "auto_rank"}) > 1:
            raise ValueError("pca: choose n, var or auto_rank")
    model = spec["model"]
    if not isinstance(model, dict) or set(model) - {"family", "params"} or model.get("family") not in MODELS:
        raise ValueError("unknown model family")
    name = model["family"]
    if spec["task_type"] not in REGISTRY[name]["task_types"]:
        raise ValueError(name + ": incompatible task_type")
    _params(name, model.get("params", {}), MODELS[name], grid)
    return spec


def validation_error(spec):
    try:
        validate(spec)
    except ValueError as exc:
        return str(exc).replace("\n", " ")[:150]
    return None


def expand(specs):
    if not isinstance(specs, list) or not specs:
        raise ValueError("add must be a non-empty list of recipe specs")
    axes_by_spec, total = [], 0
    for spec in specs:
        validate(spec, grid=True)
        axes = []
        for i, step in enumerate(spec["steps"]):
            axes += [(('steps', i, key), value) for key, value in step.items() if isinstance(value, list)]
        axes += [(('model', 'params', key), value) for key, value in spec["model"].get("params", {}).items() if isinstance(value, list)]
        count = math.prod(len(values) for _, values in axes)
        total += count
        if total > MAX_RECIPES:
            raise ValueError("recipe grid exceeds 400 recipes; refused without truncation")
        axes_by_spec.append(axes)
    result = []
    for spec, axes in zip(specs, axes_by_spec, strict=False):
        for values in itertools.product(*(axis[1] for axis in axes)):
            candidate = copy.deepcopy(spec)
            for (path, _), value in zip(axes, values, strict=False):
                target = candidate
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            validate(candidate)
            result.append(candidate)
    return result


def recipe_id(spec):
    import hashlib
    return hashlib.sha256(canonical(spec).encode()).hexdigest()[:16]
