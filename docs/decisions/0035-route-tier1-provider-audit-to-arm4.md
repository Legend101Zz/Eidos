# ADR 0035 · Route the Tier 1 provider audit to the trusted Arm 4 bridge

**Status** Proposed for the next development evaluation.

The completed development attempt recorded no active provider audit for its seven Arm 4 assignments. The mini-eval put the configuration path in its environment, and the installed worker's environment sanitizer removed it. The resulting missing coverage remains unknown for that attempt.

A live mini-eval now pins the digest of a private configuration in its attempt receipt. The launcher passes only its path through the trusted Arm 4 controller, installed adapter and bridge. The identifiers never enter model prompts or generic lab, coding or compute subprocess environments. Other study runs keep their existing behavior without this Tier 1 configuration.

The controller and bridge verify the configuration file, owner, mode, digest and binding to the attempt and arm. A supplied missing, malformed, stale or linked configuration fails before provider forwarding. The bridge attests activation and the loaded digest, and the scorecard checks both against the original attempt pin. The existing held-out-read invariant still requires complete evidence and positive request coverage. The audit retains only request digests and exact known-identifier matches.

An offline loopback check exercises launcher, controller, installed worker, adapter and real bridge startup with a substituted test setup and an unavailable loopback upstream. It verifies both a match and a clean request. No model or provider request is made.
