# Attribution, provenance and scope

The development code in this directory was newly authored for the Eidos research preparation task. The research/public subtree is authorized for public delivery; no general redistribution license is assigned here. Public availability does not grant a general license. A later explicit license decision should be recorded separately. Upstream benchmark code is referenced and inspected, not copied into this export. License statements below describe the inspected source, not legal clearance for every downstream use.

Datasets loaded through scikit-learn:

- **Iris** — Fisher, R. (1936), UCI Machine Learning Repository, DOI [10.24432/C56C76](https://doi.org/10.24432/C56C76). UCI lists CC BY 4.0. The scikit-learn bundled version and its corrected values are used; splits and readout corruption are local adaptations. [Dataset page](https://archive.ics.uci.edu/dataset/53/iris).
- **Optical Recognition of Handwritten Digits** — Alpaydin, E. and Kaynak, C. (1998), UCI, DOI [10.24432/C50P49](https://doi.org/10.24432/C50P49). UCI lists CC BY 4.0. The 1,797-image scikit-learn subset is normalized by 16, resplit, and in D03 shifted by a fixed offset. [Dataset page](https://archive.ics.uci.edu/dataset/80/optical+recognition+of+handwritten+digits).

Scikit-learn is a separately licensed dependency; its BSD license does not replace dataset attribution. Other project data are newly generated small controls with construction code exposed in `data.py`. No pretraining-contamination guarantee follows.

Inspected sources, pinned 7 November 2026:

| Source | Revision | License and current status |
|---|---|---|
| [AIRS-Bench](https://github.com/facebookresearch/airs-bench) | `18e4f1d501069cf7d7e2740d81c2ca748c56a6a1` | CC BY-NC 4.0 repository; active/unarchived at inspection; dataset licenses separate |
| [MLGym](https://github.com/facebookresearch/MLGym) | `9d40c1b5035202018cd7091fb4e83a9c68b377c0` | Archived; majority CC BY-NC 4.0 with named MIT/Apache exceptions; dataset terms separate |
| [LLM Speedrunner](https://github.com/facebookresearch/llm-speedrunner) | `ec3e4afd6dcf7c99f7b0be35898e40df6e1257c9` | CC BY-NC 4.0 repository; unarchived; model/data/dependency terms separate |
| [AIDE](https://github.com/WecoAI/aideml) | `ad73d227af481bea3de06f884f41b2993dd6453a` | MIT repository; unarchived; example-dataset rights separate |
| [RE-Bench](https://github.com/METR/RE-Bench) | `a0cc6d4a91b20f84ebef505d5c72358f88b3b389` | Archived and deprecated; MIT text plus explicit requests about protecting tasks/solutions and avoiding training/overfitting |

RE-Bench is methodological background only. Its requests include keeping protected solutions unpublished and avoiding sending evaluation material to services that may train on it; we did not extract password-protected files. An MIT label alone would not justify ignoring those requests.

Primary papers reviewed: [AIRS-Bench](https://arxiv.org/abs/2602.06855), [MLGym](https://arxiv.org/abs/2502.14499), [Automated LLM Speedrunning](https://arxiv.org/abs/2506.22419), [AIDE](https://arxiv.org/abs/2502.13138), and [RE-Bench](https://arxiv.org/abs/2411.15114). The protocol uses their task diversity, external scoring and budget-scaling lessons without treating published performance as Eidos evidence.

## Source recheck and prospective adapter admission

All five pinned revisions still matched upstream main during a second read-only review on 7 November 2026. AIRS's 24 hours is the search envelope; its [Appendix C](https://arxiv.org/html/2602.06855v1#A3) allows roughly 36 hours including evaluation for AIRA-dojo. MLGym's 1800 seconds is a per-training-command limit in selected configurations, not a campaign limit; [other task configurations](https://github.com/facebookresearch/MLGym/tree/9d40c1b5035202018cd7091fb4e83a9c68b377c0/configs/tasks) use 2400 or 3600 seconds.

| Prospective adapter | Admission requirement | Evidence status |
|---|---|---|
| AIRS solar | Label-defined denominator; reject candidate NaN/Inf | Existing numeric fixture executed; full task unqualified |
| AIRS SICK | Reject nonfinite Spearman for constant predictions | Existing numeric fixture executed; full task unqualified |
| MLGym house prices | Raw-price RMSE; exact row alignment; no Kaggle log-score equivalence | Existing numeric fixture executed; full task unqualified |
| MLGym Titanic | Unique PassengerId, exact full ID set, one-to-one alignment | Static source review only; no new task qualified |
| AIRS graph regression | Dataset rights, project splits, scalar parsing, finite predictions, CPU feasibility | Prospective coverage gap; not admitted or downloaded |

The [pinned Titanic grader](https://github.com/facebookresearch/MLGym/blob/9d40c1b5035202018cd7091fb4e83a9c68b377c0/data/titanic/evaluate.py) checks row count before an inner ID merge but lacks full ID-set/uniqueness validation. A strict adapter must fix that boundary and disclose an adaptation. This is a static finding, not an executed exploit or official benchmark result. Multiple framework representations of one AIRS task or targets from one dataset are correlated, not new independent project families. AIDE startup metric-inference, code, feedback, report and retry calls all count toward its budget; its search metric remains separate from final grading.
