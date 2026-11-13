"""Explicit construction registrations for statistical and scoring test fixtures."""
import uuid

from study.practice import guard, pipelines
from study.practice.ledger import register


def registration(classes):
    return register(evaluation_id=str(uuid.uuid4()), candidate_digest='test-fixture', classes=list(classes),
                    primary_verdict='test-only construction', kind='construction')


def evaluate(effects, roles, **options):
    return guard.evaluate(effects, roles, registration=registration(roles), **options)


def compute_table(*args, **options):
    return pipelines.compute_table(*args, registration=registration(['test']), **options)


def guard_selftest():
    return guard.guard_selftest(registration=registration([f'g{i}' for i in range(12)]))
