"""Digest-pinned source bundles and explicit-class checkpoint containers.

Restricted pickle is a robustness check, not an adversarial security boundary.
"""
import hashlib
import io
import json
import pickle
from pathlib import Path

from .spec import validate

MAX_CHECKPOINT_BYTES = 64_000_000
# Explicit globals, never a package prefix. Changes require a reviewed library release.
ALLOWED = {
    ("builtins", "slice"), ("builtins", "set"), ("builtins", "frozenset"),
    ("numpy", "ndarray"), ("numpy", "dtype"),
    ("numpy._core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "scalar"),
    ("numpy.core.multiarray", "_reconstruct"), ("numpy.core.multiarray", "scalar"),
    ("numpy._core.numeric", "_frombuffer"), ("numpy.core.numeric", "_frombuffer"),
    ("sklearn.pipeline", "Pipeline"),
    ("sklearn.preprocessing._data", "StandardScaler"), ("sklearn.preprocessing._data", "RobustScaler"),
    ("sklearn.preprocessing._data", "QuantileTransformer"), ("sklearn.preprocessing._data", "Normalizer"),
    ("sklearn.preprocessing._data", "Binarizer"), ("sklearn.preprocessing._encoders", "OneHotEncoder"),
    ("sklearn.feature_extraction.text", "TfidfTransformer"), ("scipy.sparse._csr", "csr_matrix"),
    ("sklearn.decomposition._pca", "PCA"), ("sklearn.linear_model._ridge", "Ridge"),
    ("sklearn.linear_model._logistic", "LogisticRegression"),
    ("sklearn.discriminant_analysis", "LinearDiscriminantAnalysis"),
    ("sklearn.discriminant_analysis", "QuadraticDiscriminantAnalysis"),
    ("sklearn.mixture._gaussian_mixture", "GaussianMixture"),
    ("sklearn.neighbors._classification", "KNeighborsClassifier"),
    ("sklearn.neighbors._regression", "KNeighborsRegressor"),
    ("sklearn.neighbors._kd_tree", "KDTree"), ("sklearn.neighbors._kd_tree", "KDTree64"),
    ("sklearn.neighbors._kd_tree", "newObj"),
    ("sklearn.metrics._dist_metrics", "EuclideanDistance64"), ("sklearn.metrics._dist_metrics", "newObj"),
    ("sklearn.preprocessing._label", "LabelEncoder"), ("numpy.random._pickle", "__generator_ctor"),
    ("numpy.random._pcg64", "PCG64"), ("numpy.random.bit_generator", "__pyx_unpickle_SeedSequence"),
    ("numpy.random.bit_generator", "SeedSequence"),
    ("sklearn.svm._classes", "SVC"), ("sklearn.svm._classes", "SVR"),
    ("sklearn.svm._classes", "LinearSVC"), ("sklearn.svm._classes", "LinearSVR"),
    ("sklearn.kernel_ridge", "KernelRidge"),
    ("sklearn.ensemble._forest", "ExtraTreesClassifier"), ("sklearn.ensemble._forest", "ExtraTreesRegressor"),
    ("sklearn.ensemble._forest", "RandomForestClassifier"), ("sklearn.ensemble._forest", "RandomForestRegressor"),
    ("sklearn.tree._classes", "ExtraTreeClassifier"), ("sklearn.tree._classes", "ExtraTreeRegressor"),
    ("sklearn.tree._classes", "DecisionTreeClassifier"), ("sklearn.tree._classes", "DecisionTreeRegressor"),
    ("sklearn.tree._tree", "Tree"),
    ("sklearn.ensemble._hist_gradient_boosting.gradient_boosting", "HistGradientBoostingClassifier"),
    ("sklearn.ensemble._hist_gradient_boosting.gradient_boosting", "HistGradientBoostingRegressor"),
    ("sklearn.ensemble._hist_gradient_boosting.binning", "_BinMapper"),
    ("sklearn.ensemble._hist_gradient_boosting.predictor", "TreePredictor"),
    ("sklearn._loss.loss", "HalfBinomialLoss"), ("sklearn._loss.loss", "HalfMultinomialLoss"),
    ("sklearn._loss.loss", "HalfSquaredError"), ("sklearn._loss._loss", "CyHalfBinomialLoss"),
    ("sklearn._loss._loss", "CyHalfMultinomialLoss"),
    ("sklearn._loss._loss", "__pyx_unpickle_CyHalfMultinomialLoss"), ("sklearn._loss._loss", "CyHalfSquaredError"),
    ("sklearn._loss.link", "LogitLink"), ("sklearn._loss.link", "MultinomialLogit"), ("sklearn._loss.link", "IdentityLink"),
    ("sklearn._loss.link", "Interval"),
    ("sklearn.preprocessing._label", "LabelBinarizer"),
    ("sklearn.neural_network._multilayer_perceptron", "MLPClassifier"),
    ("sklearn.neural_network._multilayer_perceptron", "MLPRegressor"),
    ("sklearn.neural_network._stochastic_optimizers", "AdamOptimizer"),
    ("numpy.random._pickle", "__randomstate_ctor"), ("numpy.random._pickle", "__bit_generator_ctor"),
    ("numpy.random._mt19937", "MT19937"), ("numpy.random.mtrand", "RandomState"),
}
ALLOWED.update(("numpy", name) for name in ("float16", "float32", "float64", "bool_", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "str_"))
ALLOWED.update(("eidos_recipes.ops", name) for name in ("RowTransform", "SignedTfidf", "VarianceScreen", "CorrDedupe", "ImportanceTopK", "ShadowScreen", "AdaptivePCA", "LowCardOneHot"))
ALLOWED.update(("eidos_recipes.models", name) for name in ("ClassGMM", "SeededMLP"))


class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) not in ALLOWED:
            raise pickle.UnpicklingError("checkpoint class not allowed: " + module + "." + name)
        return super().find_class(module, name)


def library_digest():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def loads(data):
    if len(data) > MAX_CHECKPOINT_BYTES:
        raise ValueError("checkpoint exceeds 64 MB")
    return RestrictedUnpickler(io.BytesIO(data)).load()


def save(path, model, spec, *, max_bytes=MAX_CHECKPOINT_BYTES):
    import numpy
    import sklearn
    validate(spec)
    state = dict(schema="recipe-checkpoint/1", model=model, spec=spec, library_digest=library_digest(),
                 environment=dict(numpy=numpy.__version__, sklearn=sklearn.__version__))
    data = pickle.dumps(state, protocol=5)
    if len(data) > min(max_bytes, MAX_CHECKPOINT_BYTES):
        raise ValueError("checkpoint exceeds export byte limit")
    Path(path).write_bytes(data)
    return len(data)


def load(path):
    if Path(path).stat().st_size > MAX_CHECKPOINT_BYTES:
        raise ValueError("checkpoint exceeds 64 MB")
    state = loads(Path(path).read_bytes())
    if not isinstance(state, dict) or state.get("schema") != "recipe-checkpoint/1" or state.get("library_digest") != library_digest():
        raise ValueError("checkpoint schema or library digest mismatch")
    validate(state["spec"])
    return state


def source_bundle(spec, *, basis="one-se-choice"):
    validate(spec)
    root = Path(__file__).resolve().parent.parent
    files = {"eidos_recipes/" + p.name: p.read_text() for p in sorted((root / "eidos_recipes").glob("*.py"))}
    files["project.py"] = (root / "templates/project.py").read_text()
    files["recipe.json"] = json.dumps(spec, sort_keys=True) + "\n"
    return dict(schema="source-bundle/1", files=files, library_digest=library_digest(),
                recipe=spec, basis=basis, claim=None, exploratory=True)
