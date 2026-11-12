"""Typed recipe runner for eidos.search.recipe-sweep templates (numpy; scikit-learn only for the svm and gbm families).

A recipe is {"preprocess": ..., "family": ..., "settings": {...}}. Data are NPZ arrays: train.npz and validation.npz
with x (examples first; any further dimensions are flattened), y and optionally ids. Nothing else is read.

  fit_recipe.py screen  --out DIR --seed S --params JSON --task-type T       fit on train, score visible validation
  fit_recipe.py cv      --out DIR --seed S --fold F --folds K --params JSON --task-type T
                                                                             K-fold CV within train only
  fit_recipe.py train   --inputs DIR --out DIR --seed S (--params JSON | --recipe FILE) --task-type T
                                                                             a project command: writes
                                                                             validation_predictions.npz and receipt.json
  fit_recipe.py predict --inputs DIR --data FILE --out FILE --seed S (--params JSON | --recipe FILE) --task-type T
                                                                             fit on train, predict another NPZ's x

Every metric is written to {out}/receipt.json. Deterministic for a given seed, recipe and data.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

PREPROCESS = ("none", "standardize", "per_example_center", "per_example_standardize")
FAMILIES = ("linear", "knn", "kernel", "mlp", "svm", "gbm")
SKLEARN_FAMILIES = ("svm", "gbm")
EPS = 1e-8


# -- data -------------------------------------------------------------------------------------------------------------
def load(path, need_y=True):
    with np.load(str(path), allow_pickle=False) as data:
        x = np.asarray(data["x"], dtype=np.float64)
        y = np.asarray(data["y"]) if "y" in data else None
        ids = np.asarray(data["ids"]) if "ids" in data else np.arange(len(x))
    x = x.reshape(len(x), -1)
    if need_y and y is None:
        raise SystemExit("%s has no y array" % path)
    if not np.all(np.isfinite(x)):
        raise SystemExit("%s has non-finite inputs" % path)
    return x, y, ids


# -- preprocessing: per-example transforms are stateless; standardize is fitted on the training rows only ------------
class Preprocess:
    def __init__(self, kind):
        if kind not in PREPROCESS:
            raise SystemExit("unknown preprocess %r" % kind)
        self.kind, self.mu, self.sd = kind, None, None

    def fit(self, x):
        if self.kind == "standardize":
            self.mu, sd = x.mean(0), x.std(0)
            self.sd = np.where(sd < EPS, 1.0, sd)
        return self

    def transform(self, x):
        if self.kind == "standardize":
            return (x - self.mu) / self.sd
        if self.kind == "per_example_center":
            return x - x.mean(1, keepdims=True)
        if self.kind == "per_example_standardize":
            sd = x.std(1, keepdims=True)
            return (x - x.mean(1, keepdims=True)) / np.where(sd < EPS, 1.0, sd)
        return x


# -- model families ---------------------------------------------------------------------------------------------------
def _onehot(y, classes):
    return (y[:, None] == classes[None, :]).astype(np.float64)


def _subsample(x, y, cap, rng):
    if cap and len(x) > cap:
        keep = np.sort(rng.choice(len(x), int(cap), replace=False))
        return x[keep], y[keep]
    return x, y


class Linear:
    """Ridge regression, or multinomial logistic regression with an L2 penalty (full-batch Adam)."""

    def __init__(self, task, settings, rng):
        self.task, self.alpha, self.rng = task, float(settings.get("alpha", 1e-3)), rng
        self.steps = int(settings.get("steps", 300))

    def fit(self, x, y):
        self.xm = x.mean(0)
        a = x - self.xm
        if self.task == "regression":
            self.ym = float(np.mean(y))
            d = a.shape[1]
            lam = self.alpha * len(a)
            if d <= len(a):
                self.w = np.linalg.solve(a.T @ a + lam * np.eye(d), a.T @ (y - self.ym))
            else:
                self.w = a.T @ np.linalg.solve(a @ a.T + lam * np.eye(len(a)), y - self.ym)
            return self
        self.classes = np.unique(y)
        t = _onehot(y, self.classes)
        w = np.zeros((a.shape[1], len(self.classes)))
        b = np.zeros(len(self.classes))
        m = [np.zeros_like(w), np.zeros_like(b)]
        v = [np.zeros_like(w), np.zeros_like(b)]
        scale = 1.0 / max(np.sqrt((a * a).sum(1).mean()), EPS)
        for step in range(1, self.steps + 1):
            z = (a * scale) @ w + b
            z -= z.max(1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(1, keepdims=True)
            g = (p - t) / len(a)
            grads = [(a * scale).T @ g + self.alpha * w, g.sum(0)]
            for i, (param, grad) in enumerate(zip((w, b), grads)):
                m[i] = 0.9 * m[i] + 0.1 * grad
                v[i] = 0.999 * v[i] + 0.001 * grad * grad
                param -= 0.05 * (m[i] / (1 - 0.9 ** step)) / (np.sqrt(v[i] / (1 - 0.999 ** step)) + 1e-8)
        self.w, self.b, self.scale = w, b, scale
        return self

    def predict(self, x):
        a = x - self.xm
        if self.task == "regression":
            return a @ self.w + self.ym
        return self.classes[np.argmax((a * self.scale) @ self.w + self.b, 1)]


class KNN:
    def __init__(self, task, settings, rng):
        self.task, self.k = task, int(settings.get("k", 5))

    def fit(self, x, y):
        self.x, self.y, self.x2 = x, y, (x * x).sum(1)
        if self.task == "classification":
            self.classes = np.unique(y)
        return self

    def predict(self, x):
        k, out = max(1, min(self.k, len(self.x))), []
        for start in range(0, len(x), 512):
            c = x[start:start + 512]
            d = (c * c).sum(1)[:, None] + self.x2[None, :] - 2.0 * c @ self.x.T
            nearest = np.argsort(d, axis=1, kind="stable")[:, :k]
            if self.task == "classification":
                votes = (self.y[nearest][:, :, None] == self.classes[None, None, :]).sum(1)
                out.append(self.classes[np.argmax(votes, 1)])
            else:
                out.append(self.y[nearest].astype(np.float64).mean(1))
        return np.concatenate(out)


class Kernel:
    """Kernel ridge regression with an RBF kernel (gamma 1 / (features * variance), as "scale"); one-hot targets for
    classification. At most max_rows training rows (a seeded subsample) keep it bounded."""

    def __init__(self, task, settings, rng):
        self.task, self.alpha, self.rng = task, float(settings.get("alpha", 1e-2)), rng
        self.cap, self.gamma_mult = int(settings.get("max_rows", 2000)), float(settings.get("gamma_scale", 1.0))

    def _k(self, a, b):
        d = (a * a).sum(1)[:, None] + (b * b).sum(1)[None, :] - 2.0 * a @ b.T
        return np.exp(-self.gamma * np.maximum(d, 0.0))

    def fit(self, x, y):
        x, y = _subsample(x, y, self.cap, self.rng)
        self.x = x
        self.gamma = self.gamma_mult / max(x.shape[1] * x.var(), EPS)
        if self.task == "classification":
            self.classes = np.unique(y)
            t = _onehot(y, self.classes)
        else:
            t = y.astype(np.float64)[:, None]
        self.tm = t.mean(0)
        self.c = np.linalg.solve(self._k(x, x) + self.alpha * np.eye(len(x)), t - self.tm)
        return self

    def predict(self, x):
        out = np.concatenate([self._k(x[s:s + 1024], self.x) @ self.c for s in range(0, len(x), 1024)]) + self.tm
        return self.classes[np.argmax(out, 1)] if self.task == "classification" else out[:, 0]


class MLP:
    """One hidden ReLU layer, Adam, minibatches in a seeded order, L2 weight decay; inputs scaled by the training rows'
    root-mean-square norm so the step size suits any preprocessing."""

    def __init__(self, task, settings, rng):
        self.task, self.rng = task, rng
        self.hidden, self.epochs = int(settings.get("hidden", 128)), int(settings.get("epochs", 40))
        self.lr, self.decay, self.batch = float(settings.get("lr", 1e-3)), float(settings.get("decay", 1e-4)), 128

    def fit(self, x, y):
        self.xm = x.mean(0)
        a = x - self.xm
        self.scale = 1.0 / max(np.sqrt((a * a).mean()), EPS)
        a = a * self.scale
        if self.task == "classification":
            self.classes = np.unique(y)
            t = _onehot(y, self.classes)
        else:
            self.ym, self.ys = float(np.mean(y)), float(np.std(y)) or 1.0
            t = ((y - self.ym) / self.ys)[:, None]
        d, h, o = a.shape[1], self.hidden, t.shape[1]
        params = [self.rng.normal(0, np.sqrt(2.0 / d), (d, h)), np.zeros(h), self.rng.normal(0, np.sqrt(1.0 / h), (h, o)),
                  np.zeros(o)]
        m = [np.zeros_like(p) for p in params]
        v = [np.zeros_like(p) for p in params]
        step = 0
        for _ in range(self.epochs):
            order = self.rng.permutation(len(a))
            for s in range(0, len(a), self.batch):
                idx = order[s:s + self.batch]
                xb, tb = a[idx], t[idx]
                w1, b1, w2, b2 = params
                hpre = xb @ w1 + b1
                hid = np.maximum(hpre, 0.0)
                out = hid @ w2 + b2
                if self.task == "classification":
                    out -= out.max(1, keepdims=True)
                    p = np.exp(out)
                    p /= p.sum(1, keepdims=True)
                    g = (p - tb) / len(idx)
                else:
                    g = 2.0 * (out - tb) / len(idx)
                gh = (g @ w2.T) * (hpre > 0)
                grads = [xb.T @ gh + self.decay * w1, gh.sum(0), hid.T @ g + self.decay * w2, g.sum(0)]
                step += 1
                for i, grad in enumerate(grads):
                    m[i] = 0.9 * m[i] + 0.1 * grad
                    v[i] = 0.999 * v[i] + 0.001 * grad * grad
                    params[i] -= self.lr * (m[i] / (1 - 0.9 ** step)) / (np.sqrt(v[i] / (1 - 0.999 ** step)) + 1e-8)
        self.params = params
        return self

    def predict(self, x):
        w1, b1, w2, b2 = self.params
        out = np.maximum(((x - self.xm) * self.scale) @ w1 + b1, 0.0) @ w2 + b2
        return self.classes[np.argmax(out, 1)] if self.task == "classification" else out[:, 0] * self.ys + self.ym


class Sklearn:
    """svm: SVC / SVR with an RBF kernel; gbm: histogram gradient boosting. Needs scikit-learn in this interpreter."""

    def __init__(self, task, settings, rng, family):
        try:
            from sklearn import ensemble, svm
        except ImportError:
            raise SystemExit("family %s needs scikit-learn, which this interpreter does not have" % family) from None
        seed = int(rng.integers(0, 2 ** 31 - 1))
        self.cap, self.rng = int(settings.get("max_rows", 5000)), rng
        if family == "svm":
            c = float(settings.get("C", 1.0))
            self.model = svm.SVC(C=c, gamma="scale") if task == "classification" else svm.SVR(C=c, gamma="scale")
        else:
            kw = dict(learning_rate=float(settings.get("lr", 0.1)), max_iter=int(settings.get("max_iter", 200)),
                      random_state=seed)
            self.model = (ensemble.HistGradientBoostingClassifier(**kw) if task == "classification"
                          else ensemble.HistGradientBoostingRegressor(**kw))

    def fit(self, x, y):
        self.model.fit(*_subsample(x, y, self.cap, self.rng))
        return self

    def predict(self, x):
        return self.model.predict(x)


def make_model(task, recipe, rng):
    family, settings = recipe.get("family"), dict(recipe.get("settings") or {})
    if family in SKLEARN_FAMILIES:
        return Sklearn(task, settings, rng, family)
    classes = {"linear": Linear, "knn": KNN, "kernel": Kernel, "mlp": MLP}
    if family not in classes:
        raise SystemExit("unknown family %r" % family)
    return classes[family](task, settings, rng)


# -- fitting and scoring ----------------------------------------------------------------------------------------------
def fit_predict(task, recipe, seed, xtr, ytr, xte):
    rng = np.random.default_rng(seed)
    pre = Preprocess(recipe.get("preprocess", "none")).fit(xtr)
    model = make_model(task, recipe, rng).fit(pre.transform(xtr), ytr)
    return model.predict(pre.transform(xte))


def metrics(task, y, pred, prefix):
    if task == "classification":
        return {prefix + "_accuracy": float(np.mean(pred == y))}
    resid = pred.astype(np.float64) - y.astype(np.float64)
    return {prefix + "_rmse": float(np.sqrt(np.mean(resid ** 2))), prefix + "_mae": float(np.mean(np.abs(resid)))}


def _recipe(args):
    if args.recipe:
        return json.loads(Path(args.recipe).read_text())
    return json.loads(args.params)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("screen", "cv", "train", "predict"))
    parser.add_argument("--inputs", default=".")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fold", type=int, default=0)
    parser.add_argument("--folds", type=int, default=1)
    parser.add_argument("--params")
    parser.add_argument("--recipe")
    parser.add_argument("--data")
    parser.add_argument("--task-type", choices=("classification", "regression"), required=True)
    args = parser.parse_args(argv)
    if not (args.params or args.recipe):
        parser.error("give --params or --recipe")
    recipe, task, inputs = _recipe(args), args.task_type, Path(args.inputs)
    xtr, ytr, _ = load(inputs / "train.npz")
    if args.mode == "predict":
        x, _, ids = load(args.data, need_y=False)
        np.savez(args.out, ids=ids, predictions=fit_predict(task, recipe, args.seed, xtr, ytr, x))
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    receipt = {"seed": args.seed, "train_rows": int(len(xtr))}
    if args.mode == "cv":
        if not 0 <= args.fold < args.folds or args.folds < 2:
            raise SystemExit("cv needs 0 <= fold < folds and folds >= 2")
        if args.folds > len(xtr):
            raise SystemExit("cv needs at most one fold per training row (%d folds, %d rows)" % (args.folds, len(xtr)))
        parts = np.array_split(np.random.default_rng(args.seed).permutation(len(xtr)), args.folds)
        held = parts[args.fold]
        fit = np.concatenate([p for i, p in enumerate(parts) if i != args.fold])
        receipt.update(metrics(task, ytr[held], fit_predict(task, recipe, args.seed, xtr[fit], ytr[fit], xtr[held]),
                               "train_cv"))
    else:
        xva, yva, ids = load(inputs / "validation.npz")
        pred = fit_predict(task, recipe, args.seed, xtr, ytr, xva)
        receipt.update(metrics(task, yva, pred, "validation"), validation_rows=int(len(xva)))
        if args.mode == "train":
            np.savez(out / "validation_predictions.npz", ids=ids, predictions=pred)
            (out / "recipe.json").write_text(json.dumps(recipe, sort_keys=True))
    (out / "receipt.json").write_text(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
