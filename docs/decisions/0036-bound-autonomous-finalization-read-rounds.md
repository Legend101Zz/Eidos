# ADR 0036 · Bound read rounds in autonomous finalization

**Status** Proposed for a subsequent development build.

In the settled D02-r1 development run, a normal turn ended with invalid structured output. Its single autonomous finalization decision used six provider responses to request 24 authorized evidence reads. The token envelope refused the next request before the model attempted a conclusion. The controller-authored abstention and the settled result remain immutable.

An autonomous conclude-only decision allows at most two read-enabled rounds and at most four reads per round. The next request accepts only a model-authored conclude or stop with a rationale and no effects. It uses the evidence already returned, with read tools removed from the request and read proposals refused. One bounded decision-only correction may follow a malformed reply if it fits the original request, token and wall ceilings. The runtime records a typed failure when a final request cannot fit or the model supplies no valid conclusion. The change leaves ordinary exploration and evidence authority in place.

The two-round allowance is a prospective design choice. An offline replay retains the first two exact D02 replies and their returned evidence, then uses explicitly synthetic post-divergence replies to exercise admission and refusal. It gives no counterfactual live result.
