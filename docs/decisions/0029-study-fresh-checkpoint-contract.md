# 0029 State the study's fresh-checkpoint filename contract

Accepted prospectively for the Pilot 4 study controller. The settled Pilot 3 N04-r3 arm-3 submission declared one locked checkpoint basename while its fresh-training command wrote another. The committed local and hosted grading convention already looks for the declared basename in each seed's output directory. The shared model prompt specified the reproduction arguments and output directory but did not state the filename convention.

## Decision

Every arm receives the same instruction in the shared study prompt. The fresh-training command writes `{out}/<basename of candidate.model_path>` for every requested seed, with a checkpoint the submitted infer command can load. The tool-facing charter also records the contract. Tier 1 uses the same revised controller for both frozen builds.

The change clarifies the existing grader interface. Pilot 3's frozen prompts and recorded grades retain their original provenance. The sealed task bundles, practical bars, grading rules and core runtime remain fixed. The adapter tests capture the coding CLI requests and the investigator question without calling a model.
