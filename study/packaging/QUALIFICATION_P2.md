# Pilot 2 baseline qualification

Packaged baseline seed 11; read-only public reference, fresh seeds 11/23/37. CPU, one thread.

|Card|Metric|Baseline|Reference seed 11|Gain seed 11|Fresh-seed gains 23,37|Bar|
|---|---|---:|---:|---:|---|---:|
|D03|accuracy|0.691666667|0.947222222|0.255555556|0.258333333, 0.255555556|0.01|
|D04|rmse|2.113944681|0.506770590|1.607174091|1.291967894, 1.610948419|0.5|
|D05|rmse|0.940362773|0.022705007|0.917657767|0.918515863, 0.918185503|0.5|
|D06|accuracy|0.503906250|1.000000000|0.496093750|0.496093750, 0.496093750|0.3|

All reference seeds clear the practical bar. No model calls. Reproduce: source ~/eidos-internal/env.sh; "$EIDOS_RESEARCH_PYTHON" -m study.packaging.qualify_p2 EMPTY_DIR
