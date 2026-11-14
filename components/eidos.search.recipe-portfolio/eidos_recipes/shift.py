"""Train/visible-validation domain discrimination, shared with a future profile."""
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def delong_auc(labels, scores):
    """One-AUC DeLong placements with tie credit; no quadratic placement matrix."""
    positive, negative = scores[labels == 1], scores[labels == 0]
    a, b = np.sort(positive), np.sort(negative)
    v10 = (np.searchsorted(b, positive, side="left") + np.searchsorted(b, positive, side="right")) / (2 * len(b))
    v01 = 1 - (np.searchsorted(a, negative, side="left") + np.searchsorted(a, negative, side="right")) / (2 * len(a))
    auc = float(v10.mean())
    se = float(np.sqrt(v10.var(ddof=1) / len(a) + v01.var(ddof=1) / len(b)))
    return dict(auc=auc, se=se, lower95=auc - 1.959963984540054 * se, upper95=auc + 1.959963984540054 * se)


def detect(train, validation, *, seed=81723):
    rng = np.random.default_rng(seed)
    # Bounded check with balanced domain priors; no target labels.
    a = train[rng.permutation(len(train))[:1500]]
    b = validation[rng.permutation(len(validation))[:1500]]
    x, labels = np.vstack((a, b)), np.r_[np.zeros(len(a)), np.ones(len(b))]
    p = np.empty(len(labels))
    for tr, va in StratifiedKFold(3, shuffle=True, random_state=seed).split(x, labels):
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=300, random_state=seed)).fit(x[tr], labels[tr])
        p[va] = m.predict_proba(x[va])[:, 1]
    result = delong_auc(labels, p)
    return dict(**result, shift_detected=result["lower95"] > .6, threshold=.6,
                method="logistic out-of-fold domain AUC; single-AUC DeLong interval")
