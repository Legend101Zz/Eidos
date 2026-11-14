# Mechanical method catalogs

A catalog records the methods a particular build offers without authoring:
recipe registry operators and model names, instantiated scientific-library
atoms, sweep families, component releases, and normalised AST hashes of library
and export-template functions/classes. It includes source digests and its own
canonical SHA-256 digest. Generate one locally with:

```sh
python -m study.exp.novelty catalog --out experiments/catalog/build.json
```

Candidates are parsed as data; their code is never imported or executed.
Normalisation strips docstrings/comments and renames local bindings while
preserving attributes and call names. Renaming variables does not earn novelty.

| Class | Mechanical evidence |
|---|---|
| catalog | Intact template/library export selected by the portfolio's `one-se-choice` rule. |
| recombination | Known atoms combined with authored glue, changed template code or selection outside the rule. |
| external | An outside estimator/transform constructor, authored estimator class, or authored fit/predict/transform logic. |

Parse failures, missing catalog components and ambiguous evidence produce
`unknown`, which counts as catalog for scoring. Class labels alone are not wins:
a novelty win also needs verified success and an improvement over matched
autopilot predictions greater than one paired standard error on the same rows.
Prediction distance is descriptive only. A model never judges novelty.
