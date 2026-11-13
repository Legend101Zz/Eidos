# Pilot 4 Addendum 2 — attested pay-as-you-go admission

Prepared prospectively on 13 November 2026 against R2
`8873cc0e667bf6bee09d2137810f16903eb443e6` (Addendum 1). This text is an
uncommitted draft. It must be registered and published, and the exact resulting
registration and v10.3 tooling qualified, before it affects any block. Preparation
and synthetic qualification confer no admission authority.

The owner directed Pilot 4 to continue using owner-funded MiniMax pay-as-you-go
credits when token-plan allowance is exhausted. This addendum changes only the
allowance basis and adds a bounded response to repeated quota/billing incidents.
All other charter rules, the Addendum 1 admission window, roster, randomized
order, two arms, envelopes, served-model barrier, zero-active batch boundary,
grading, replacement rules, decision rule and reporting limitations remain in force.

Every admission still reads and preserves a fresh, valid token-plan observation.
The active interval and weekly windows must remain unexpired, with valid statuses
and finite percentages in the provider's valid range. Invalid, missing, stale,
future-dated or failed observations never authorize a block. No token-spending
probe is permitted.

When both remaining percentages are strictly positive, the admission basis is
`token-plan`. When the interval is zero, admission waits for its reset, even with an
attestation. Only when the interval is strictly positive and the weekly
percentage is zero, the additional basis is
`payg-attested`: a trusted owner statement recorded by the orchestrator in UTC,
scoped to Pilot 4, in the private registration directory's
`PAYG_ATTESTATION.json`. The independently frozen SHA-256 must match its exact
bytes; the attestation must already exist at decision time and must not be
revoked or expired. An absent, changed, revoked, expired, wrongly scoped or
otherwise invalid attestation leaves the existing refusal in force. The receipt
binds both the quota observation and the attestation by SHA-256. An immutable
copy of the attestation accompanies the decision evidence; the current private
attestation is checked again before workers dispatch so a subsequent revocation
or change stops dispatch. Renewal or replacement requires a new prospective
binding. Positive token-plan admission does not depend on an attestation.

Mid-run provider quota or billing errors remain terminal provider incidents.
The five-minute cooldown, whole-block replacement, preservation of both arms
and all attempts, and existing review requirements remain unchanged. After two
consecutive terminal quota/billing provider incidents, admissions pause and a
NEEDS notice states that credits may be missing. There is no automatic replay
beyond that cap. A terminal result outside the counting rules below breaks the consecutive
sequence. Incidents count once per block attempt, in
terminal-time order with block ID as the deterministic tie-breaker, across
batches and restarts. Only the orchestrator's acknowledgement referencing a NEW owner credit
attestation version (`registration/PAYG_ATTESTATION.vN.json`, N >= 2) can clear
the cap. Its exact SHA-256, new owner statement and UTC recording after the
capped attempts, the current NEEDS hash, acknowledgement time and previous
binding are retained in an immutable credit-recovery receipt. One UTC
application timestamp is captured for both this receipt and its exact matching
orchestrator acknowledgement. The new binding
applies prospectively to decisions after that acknowledgement, rooted in the
original frozen attestation. Generic acknowledgements, the same attestation
under a new filename, elapsed time and quota observations cannot clear the cap.
The interval-zero wait, normal cooldown and every other gate still apply.
A new pair of credit incidents requires another new version and acknowledgement.

MiniMax's HTTP 429 `rate_limit_error` with numeric code 2056 ("Token Plan usage
limit reached: Upgrade your Token Plan or purchase Credits for more usage.")
is a credit/quota signal. Code 2062 ("Token Plan rate limit reached: Upgrade
your Token Plan or switch to pay-as-you-go API usage.") is an interval rate
limit: a provider incident with the normal five-minute cooldown, never a
credit-cap signal. Under `payg-attested`, every terminal provider incident with an unrecognised
message also counts toward the same two-incident pause, including an unknown
429/rate-limit code. Different unknown messages count together; 2056/billing
and unknown PAYG incidents may form the same consecutive sequence. Recognised
2062 breaks that sequence and retains its normal cooldown; successful and
other non-counting terminal results also break it. The pause emits NEEDS and
survives restart, with no automatic replay. It uses the same NEW owner
attestation and orchestrator acknowledgement recovery rule above. Under
`token-plan`, unrecognised messages retain the ordinary provider cooldown.
Typed HTTP 429 `rate_limit_error` / `ModelRateLimitError` controller failures
are classified as provider incidents before the generic infrastructure-error
rule; 2056 is a credit signal and 2062 is an interval incident.

The versioned successor is private tooling v10.3, copied from the selected
v10.2 closure. Reconciliation verifies the quota, decision basis and frozen
attestation bytes for each attempt, including the dispatch-time validity, and
refuses admissions past the incident cap. Capture reports each block attempt's
admission basis, quota hash, attestation hash and "admitted under" generation.

The registered chain retains R2's FREEZE.v2/ACTIVATION.v2 and complete v10.2
executable closure by SHA-256, alongside R3's FREEZE.v3/ACTIVATION.v3 and v10.3.
Each block is checked against the freeze active at its admission, including the
matching staging, admission and design helper hashes and registered charter
commit. R2's settled attempts remain under R2; they are never relabelled R3.
R2 admissions and replays remain governed by R2, without a retroactive R3 cap.
Before the first R3 admission, the trailing R2 terminal credit-incident sequence
is counted in the same terminal-time/block-ID order. A trailing count of two
or more requires the NEW-attestation recovery acknowledgement before R3 may
admit; one trailing incident carries into R3's sequence. Earlier R2 pairs
followed by a sequence-breaking terminal result do not latch the R3 pause.
At the switch the orchestrator first stops the v2 watchdog, then drains and
stops the v2 parent, waits for `stopped_resumable` and zero active workers and
owned jobs, and exclusive-creates `control/freeze-switchover.json`. This immutable
receipt records last R2 batch, first R3 batch, both freeze hashes, UTC switch
time and a hashed immutable snapshot of the settled v2 state and terminal
attempts. The v3 parent verifies and adopts that state without re-admitting
settled blocks. Mixed captures without the receipt, with changed predecessor
bytes, or with any attempt claimed under the wrong generation refuse.
All-R2 and all-R3 captures also verify under this chain; a receipt is mandatory
for an actual takeover of nonempty R2 state, even before its first R3 admission. Incomplete censuses identify
observed bases as unverified. Final reporting must disclose the basis per block,
all retained attempts and provider incidents, without interpreting an owner
attestation as a provider balance measurement or a guarantee of successful billing.

No other charter clause is amended. This addendum prospectively supersedes the
strictly-positive allowance requirement only in the attested case described above.

## Prospective executable binding

These reviewed successor hashes are registered by this addendum before use.
The private preparation FREEZE/ACTIVATION retain an unresolved successor commit
until this addendum is committed; they grant no launch authority.

| Artifact | SHA-256 |
| --- | --- |
| v10.3 executable manifest | `1b0bd828088eb3ae83085623b61c45f9ab551104447044549818c0c463da1866` |
| v10.3 guard | `b7aa7d917f983b6fce428248aea3fdba779bcf9ef5f14538878542741adebd5a` |
| v10.3 PAYG policy | `ac74d9923ced53208252f1f9aa88fe50020c2f86526d7bd34bc3c61e5ab01b6f` |
| v10.3 freeze-chain verifier | `72c33c1331e196c7967defd5ff08edc286044ca88c474bb897441634cfe38b01` |
| Private owner attestation (contents retained privately) | `10076f7f78a5684a1ea4fc248d6d517bac34fc3b87b677c8e844e3017abed697` |

R2 predecessor FREEZE.v2 SHA-256: `9f70f21d8c2cc8ccd35975d0641c58cac59b7ce24bda2f4d82d0f3a7f33d35f8`.
R2 predecessor ACTIVATION.v2 SHA-256: `2561142a570e4919b83a9bafcaca1ef2d42fb5a6eeb3ad1b203e7fd98c1a960e`.
