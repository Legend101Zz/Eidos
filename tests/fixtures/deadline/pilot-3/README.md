# Pilot 3 failure tails

The four behavioral fixtures project T1's committed extraction at `96966f3ed8d4fbe06d9a3e8a6d7ca2e31633837d`. Their source entries identify the T1 fixture, pilot transcript, pilot usage file and frozen Eidos build by hash. Every retained response artifact was checked against the original SHA-256 blob, and each usage event was matched by sequence to the original transcript and by value to the exported usage file.

Each fixture preserves the final three turns' decision records, full response messages, per-request timestamps and associated usage events. Request prompts and earlier decisions are omitted. The three turns contain six runtime-origin deadline placeholders in each crash. N04-r2 also has one accepted decision immediately before the placeholders. The tests replay these messages with elapsed times, remaining times, request timeout and write margin scaled together. Read stubs return the original recorded read-receipt metadata. Historical effects stay offline.

When the corrected runtime admits a request that the historical runtime never sent, a clearly synthetic reply marks that divergence. The test preserves the observed refusal sequence so that the coordinator enters its one recovery. It supplies no historical recovery response, and the durable controller abstention preserves the failure and lock without asserting investigator completion or independent confirmation. The locked candidate used to exercise controller closure is synthetic.

The N02-r2 fixture preserves the terminal provider quota receipt from its verified transcript and usage files. The terminal request yielded no assistant reply. The coordinator test uses that receipt to reproduce the typed provider failure without external calls.
