# Commit ID map (history rewrite of 2026-11-09)

On 2026-11-09 the repository history was rewritten to normalise commit metadata: author, committer and tagger identities, and message trailers. **File contents did not change.** Every rewritten commit has the same tree as before, and every branch and tag points to the same tree with the same number of commits. The tags `eidos-v0.1.0` to `eidos-v0.7.0` were re-created on the rewritten commits.

Evidence, receipts and documents written before that date cite the earlier commit IDs. [`commit-id-map.tsv`](commit-id-map.tsv) maps every earlier ID to its current one. Look up a full ID, or the abbreviated prefix that a document cites.

Notable mappings:

| What | Earlier ID | Current ID |
|---|---|---|
| Public research release `research-public-v0.1.0` (pinned source commit) | `c0c6d7ad09fb1c92296f27b4172ed6fdf5412138` | `6a6594af0e6a1f4baa429a4c080122c023a0cce8` |
| `eidos-v0.7.0` | `739cf58aea32bd5bf91d71a898d33279304cd3f6` | `aa0f4982c6ed7587300db3f776ca1382d2b543ce` |
| G9-verified release tree `ac30bfc3` | `008883cfac180e5bb019ddba6d06eee4a993ac09` | `027d474b895b7f62f4589b8c82ca12e931f47749` |

To check a mapping, compare trees: `git rev-parse <current>^{tree}` equals the tree recorded with the earlier ID.
