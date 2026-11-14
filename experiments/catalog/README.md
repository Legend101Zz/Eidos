# Mechanical method catalogs

A catalog records the methods a particular build offers without authoring:
recipe registry operators and model names, instantiated scientific-library
atoms, sweep families, component releases, and normalised AST hashes of library
and export-template functions/classes. It includes source digests and its own
canonical SHA-256 digest. Generate one locally with:

```sh
python -m study.exp.novelty catalog --commit BUILD_COMMIT --out experiments/catalog/build.json
```

Candidates are parsed as data; their code is never imported or executed.
Normalisation strips docstrings/comments and renames local bindings while
preserving module names, attributes and called library names. Callable local
bindings and closures are renamed along with other locals. Renaming variables does not earn novelty.

| Class | Mechanical evidence |
|---|---|
| catalog | Intact template/library export selected by the portfolio's `one-se-choice` rule. |
| recombination | Known atoms combined with authored glue, changed template code or selection outside the rule. |
| baseline | Byte-identical supplied baseline project; counted separately and never a novelty win. |
| external | An outside estimator/transform constructor, authored estimator class, or authored fit/predict/transform logic. |

Parse failures, missing catalog components and ambiguous evidence produce
`unknown`, which counts as catalog for scoring. Class labels alone are not wins:
a novelty win also needs verified success and an improvement over matched
autopilot predictions greater than one paired standard error on the same rows.
Prediction distance is descriptive only. A model never judges novelty.


Run scorecards use the catalog of the recorded build, read with `git show` without
checking out or executing its code. `scorecard --catalog FILE` requires that
catalog's recorded commit and digest to match the run build. Comparisons that
evaluate novelty clauses require identical catalog digests.

An edited `recipe.json` under an otherwise intact `one-se-choice` stamp is not
detected: this tool fingerprints Python source, not the recipe selection data.
For pools other than D, candidate-relative file names are hashed in scorecards.
