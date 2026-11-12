"""Trusted pilot-3 numeric scoring; candidate artifacts are only NPZ data."""
import sys

from study.common import DEVELOPMENT
from study.packaging.p3_data import TASKS, dataset

sys.path.insert(0, str(DEVELOPMENT))
from grade import read_predictions, score


def compare(project, baseline, candidate):
    card = next(c for c, p in TASKS.items() if p == project)
    data, kind, classes = dataset(card)
    _, y, ids = data['test']
    b = score(y, read_predictions(baseline, ids, kind, classes), kind)
    c = score(y, read_predictions(candidate, ids, kind, classes), kind)
    return dict(metric='accuracy' if kind == 'classification' else 'rmse', baseline=b,
                candidate=c, gain=c-b if kind == 'classification' else b-c, n=len(y),
                split='test', status='numeric development result; no causal or system certification')
