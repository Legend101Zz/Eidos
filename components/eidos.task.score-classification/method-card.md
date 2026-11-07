# eidos.task.score-classification — method card

**Question:** accuracy of integer class predictions on a split whose labels the caller may see (validation). Never a final grader.
**Checks:** exact ID-set equality, no duplicates, finite integer predictions in class range. Rows may be reordered.
**Outputs:** report (accuracy, Wilson 95% interval) and per-example correctness for paired comparisons.
**Limits:** validation was used for selection, so it is not independent confirmation. Fixtures are author tests.
