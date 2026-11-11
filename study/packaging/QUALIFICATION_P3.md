# Pilot 3 prospective qualification

Procedure fixed before any reference execution in commit `879c0942128f76e69a5a48d04ce228338d4a2551`. See [PROSPECTIVE_P3.md](PROSPECTIVE_P3.md) for the exact generators, split rounding, baseline and fixed references. No recipe was changed after seeing results. No cards were dropped. No model calls.

Execution: read-only research Python; CPU, one thread; macOS `sandbox-exec` with `(deny network*)` around the entire qualification process. CPython and library versions are recorded in [p3_runtime.json](p3_runtime.json). Raw metrics, all timings and every train/validation/test array hash are in [p3_frozen.json](p3_frozen.json). Hashes encode `str((shape, dtype.str))` followed by contiguous array bytes. Test arrays were never packaged.

Baseline: raw features, single linear layer, Adam lr=0.01, 80 full-batch epochs. The default load_diabetes representation is used without extra scaling. The gain comparator is always baseline seed 11; same-seed baseline diagnostics are also recorded. Seeds 11/23/37 are fresh initializations; deterministic references legitimately produce identical metrics across seeds.

|Card|Train / validation / test|Metric|Seed-11 baseline|Absolute bar|
|---|---|---|---:|---:|
|N01|341 / 114 / 114|accuracy|0.91228070175438591|0.02|
|N02|265 / 88 / 89|rmse|178.0077677838467|8.9003883891923348|
|N03|1200 / 400 / 400|rmse|11.042782538846298|0.55213912694231493|
|N04|1800 / 600 / 600|accuracy|0.68833333333333335|0.02|

Classification bars are +0.02 accuracy; regression absolute bars are exactly 0.05 × baseline seed-11 test RMSE. Every reference below passes its frozen bar.

|Card|Seed|Fixed baseline comparator|Diagnostic baseline at seed|Reference|Gain|Baseline process seconds|Reference fit/predict seconds|Pass|
|---|---:|---:|---:|---:|---:|---:|---:|---|
|N01|11|0.91228070175438591|0.91228070175438591|0.97368421052631582|0.061403508771929904|1.374918292|0.017424833|yes|
|N01|23|0.91228070175438591|0.83333333333333337|0.97368421052631582|0.061403508771929904|1.141724625|0.001796000|yes|
|N01|37|0.91228070175438591|0.85087719298245612|0.97368421052631582|0.061403508771929904|1.095244250|0.001667125|yes|
|N02|11|178.0077677838467|178.0077677838467|52.011559852970734|125.99620793087597|1.101176916|0.007216709|yes|
|N02|23|178.0077677838467|177.77617924501973|52.011559852970734|125.99620793087597|1.091185875|0.001069125|yes|
|N02|37|178.0077677838467|178.07065590835157|52.011559852970734|125.99620793087597|1.087791250|0.000933083|yes|
|N03|11|11.042782538846298|11.042782538846298|1.4304120554527633|9.6123704833935353|1.097057333|0.075175000|yes|
|N03|23|11.042782538846298|11.023737510584956|1.4304120554527633|9.6123704833935353|1.356372917|0.070279916|yes|
|N03|37|11.042782538846298|10.864258361476159|1.4304120554527633|9.6123704833935353|1.209007000|0.070304084|yes|
|N04|11|0.68833333333333335|0.68833333333333335|0.87|0.18166666666666664|1.155740625|0.046359666|yes|
|N04|23|0.68833333333333335|0.69666666666666666|0.87|0.18166666666666664|1.102703875|0.042914458|yes|
|N04|37|0.68833333333333335|0.69833333333333336|0.87|0.18166666666666664|1.111739333|0.042683208|yes|

All 12 baseline training processes completed in under 60 seconds; measured wall time includes Python startup. References used only the train split for fitting. The train-only StandardScaler/SVC, StandardScaler/Ridge and fixed histogram gradient booster are specified in the prospective record.

Packaging: committed bundles under `bundles/N01`–`bundles/N04` have exactly `brief.md`, `project.py`, `train.npz`, `validation.npz`, `requirements.lock`, `task.json`, `answer_schema.py`. Each task manifest hashes the other six files, gives the metric direction, and excludes the test split and reference recipe. The answer schema is copied unchanged from public development.

Prospective freeze: the cards, numeric bars, split hashes, baseline, and packaged investigator bundles are fixed by the commit containing this record before any arm run, including infrastructure stubs. The earlier recipe-selection commit predates all reference qualification. Later qualification evidence may be appended without changing cards, recipes or bars.

Local study wiring: accepted N01–N04 use the unchanged D02–D06 success rule and mandatory fresh confirmations at 23/37. N01/N04 use the I4 classification accuracy path. N02/N03 document the existing missing RMSE assessor and remain exploration-only in arm 4.

Hosted qualification and stub dry-run results will be appended after the card/bar freeze commit.
