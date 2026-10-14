# Self-hosted moderation replacement — implementation in progress

## Confirmed policy

- Block clear nudity, pornography/explicit sexual content and sexual exploitation
  of minors before publication.
- Uncertain adult sexual-content findings remain visible with a moderator flag.
- Suspected sexual exploitation of minors is hidden pending review (explicitly
  approved exception). Do not infer a child's age from appearance alone.
- No automatic blocking category for hate, profanity or viewpoints.
- An unavailable or incomplete detector is not a negative result and must not
  silently publish unexamined media.

## Implemented so far

`core/local_moderation_policy.py` defines deterministic allow/flag-visible/hold/block
decisions, precedence, a policy version and bounded non-content evidence IDs.
`core/test_local_moderation_policy.py` tests the policy, safety exception, invalid
inputs, incomplete scans and deduplication. This is not a classifier and is NOT
wired into publication. Existing production behavior is unchanged.

## Required before activation

1. Provision and validate a self-hosted text/image detector. Select licensed models
   and benchmark fitness imagery (swimwear, exposed torsos), explicit material,
   ambiguous cases and non-English text. No model weights or sensitive evaluation
   material have been downloaded. Nudity detection cannot establish exploitation
   or age. Child-safety assessment needs independently evaluated signals and an
   appropriate specialist escalation process, not a keyword/age-guess shortcut.
2. Integrate bounded image processing and video screening into the existing worker
   pipeline; never approve a whole video from its thumbnail alone. Pending or
   failed scans must not expose uploaded media through alternate delivery paths.
3. Persist machine-generated cases separately from user reports (no fake reporter),
   recording policy/model version, reason and status. Reuse moderator authorization
   and audited decisions. Atomically save decision and visibility with publication;
   recheck on edits, media replacement, reposting and worker retries.
4. Extend the private review UI for machine flags and held-content release. Account
   for races with manual takedowns; a delayed worker must not undo moderator action.
5. Update client disclosure/capability behavior and legal text without falsely
   claiming content still goes to OpenAI. Keep OpenAPI/generated clients aligned.
6. Add end-to-end tests and deployment checks proving the local detector is ready
   before selecting it. Remove OpenAI dependency only after equivalent controls are
   operational. Do not simply silence core.E006 or turn moderation off.

## Deployment limitation

Read-only inspection on 14 October found about 2 GB RAM, one CPU and no swap
(about 1.4 GB available at that instant). Validate model memory/latency under load;
media inference may require a separate private worker to avoid delaying API calls.
No external service was provisioned, no billing incurred, and no production
settings were changed. Classifier accuracy and performance remain unmeasured.
