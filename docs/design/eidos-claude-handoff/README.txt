Eidos — Claude implementation handoff

Purpose
This is an allowlisted public package for building Eidos in https://github.com/Legend101Zz/Eidos using Claude Managed Agents. It supplies the finalized local-v1 architecture, already explored development examples, a local setup prompt and a cloud implementation prompt. It contains no newly sealed evaluation tasks, private grader answers or credential values. Managed hosting is for the builder; Eidos remains a locally usable product.

Use
1. In local Claude Code, ask it to read and execute LOCAL_CLAUDE_MANAGED_SETUP_PROMPT.txt from this directory. It includes the exact repository, source path, Keychain service names, cloud setup, budget controls, CI and execution instructions.
2. The local operator verifies current SDK/account support, transfers this allowlisted package, configures the cloud session and gives CLAUDE_BUILD_PROMPT.txt to its Opus lead. Preserve this directory and architecture/ subdirectory, normally under docs/design/ in the repository.
3. Use the actual session ID and controller commands returned by that operator to inspect, interrupt or resume. The package itself does not contain a deployed controller or an already-running session.
4. Provide only reviewed public research handoffs when the independent research session prepares them. Do not upload that session's entire workspace.

Budget and models
The requested lead is claude-opus-5-5, with bounded claude-sonnet-5-5 helpers. Proposed allocation: $150 initial shared session cap, at most $10 ancillary calls, $40 unallocated reserve within a $200 total campaign. Native caps allow in-flight overshoot, so this is a conservative launch policy, not an exact billing guarantee. Verify actual controls and available funds at launch. Batch API discounts do not apply to native managed sessions. MANAGED_BUILD_NOTES.txt links current primary documentation and distinguishes verified facts from our proposed operating policy.

No paid work has been launched, Keychain secret read, remote repository modified or controller runtime-tested merely by preparing this package. The setup prompt authorizes the local Claude operator to perform the specified launch when the user gives it that prompt. Full completion within $200 is an aim, not a verified estimate.

Architecture
architecture/model-research-harness-design.md is the authoritative Revision 4 source.
architecture/model-research-harness-design.html renders all nine diagrams and the interactive component illustration. Relative artifact links resolve within architecture/.
The source Markdown includes placeholders for figures and a wire example; use the HTML for the complete rendered view.

The code in the example ZIP files demonstrates earlier local experiments. It is not an implemented Eidos service and is not hidden evaluation data. The full development task collection and study protocol are being prepared separately; no final research success threshold, task count, paid compute allocation or preregistration is claimed by this package.

Integrity
manifest.json lists SHA-256 checksums for all other files in this directory. Compare these after transfer. The manifest itself is not self-hashed. The archive is a transport snapshot, not a signed release or an external timestamped preregistration.

Versioning
Use Git commits/tags for implementation and protocol/configuration identities, plus manifests for external artifacts and runtime conditions. Do not move evaluated tags. The user has created Legend101Zz/Eidos; this package has not pushed code or created a release tag there.

Research separation
The hosted builder should receive only this archive and later explicitly public handoffs. Separate chat names and Git branches do not enforce privacy. Keep evaluation files, reference solutions and private credentials out of the builder's filesystem, Git history and connected storage. Public benchmark exposure must still be recorded honestly.
