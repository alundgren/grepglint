# Independent review, round 1

Reviewed commit: `90f5a9cdda6eee6d18e0e4ac8b8287ce32eb9f13`.
Fixed base: `ea3fa9e260288ae8ed5965cfa10113a1e640c23e`.
Reviewer thread: `/root/issue_14/completion_reviewer`.
Requested model/effort: `gpt-6-astra` / `low`; runtime model and effort were not reported. No reviewer scout was used.

This record preserves the review judgment and task receipts. File references are relative to the corpus; the complete review response also remains in the persistent review thread.

## Plan: pass

The smallest viable plan is to keep the questions, originals, source identities, factual claims and citation groups under `benchmarks/` with a frozen partition; prepare complete disposable trees and record actual coverage including failures; validate metadata and source bytes offline and require independent audit receipts before acceptance. The three Python tools and retained evidence support this plan without production dependencies. The scoring contract separates factual correctness from retrieval measurements.

The reviewer independently audited all 40 reference answers, required claims and cited regions. For published tasks, it also read the preserved questions and oracle files. Source-aware authoring validation passed. All retained notices matched the prepared source files, and applying every recorded mirror difference reconstructed all nine upstream tree IDs.

## Blocking findings

- **R1-F1, P2 — django-modelchoice-fk-fix-001:** The claim that invalid or missing values raise invalid_choice conflicts with [models.py lines 1580–1600](../evidence/django-674eda1c/django/forms/models.py#L1580). Empty values return None before lookup. Correct the claim and reference answer to distinguish empty values from failed lookups and the listed conversion/validation errors. Existing evidence covers the correction.
- **R1-F2, P2 — django-composite-field-recover-001:** A bound nonempty form is not the actual condition for cleaning. [forms.py lines 324–339](../evidence/django-674eda1c/django/forms/forms.py#L324) returns early for unbound forms and when empty_permitted is true and has_changed() is false. Submitted values equal to nonempty initial values can skip cleaning. Correct the claim and reference answer using those predicates.
- **R1-F3, P2 — numpy-rolling-median-feat-001:** The flattened-view wording promises memory sharing that ravel does not guarantee. [The median implementation lines 4026–4034](../evidence/numpy-v2.2.2/numpy/lib/_function_base_impl.py#L4026) partitions a directly when an axis is supplied or the result of a.ravel() when axis is None. Use that description; no extra mandatory evidence is needed. The reviewer also checked the ravel documentation at pinned numpy/_core/fromnumeric.py line 1905.

## Nonblocking suggestion

- **R1-S1:** Preparation checks current disk usage before export and again after export, extraction and local Git initialization. Initial reservation and fetch polling help, but disk consumed by another process during later phases is detected afterward. Refresh reservations before each write phase or describe the checks as sampled safeguards rather than hard peak guarantees.

There are no additional blocking UX findings in documentation, errors, scoring or failure reporting. Pending audit fields were expected.

## Accepted receipts: 37 tasks

For every task below, the reviewer accepted the required claims and reference answer as supported by its recorded source regions, and accepted its adaptation from the preserved original where applicable.

| Task ID | Audited behavior |
| --- | --- |
| django-pre-validate-signal-design-001 | Model validation order and pre_save dispatch |
| django-rate-limit-design-001 | Request short circuit and response hook |
| django-rate-limit-middleware-feat-001 | Default cache construction and middleware hooks |
| django-request-factory-refac-001 | Public export and HTTP request construction |
| django-audit-trail-implement-001 | Isolated include context and dictionary reset |
| django-cross-team-boundary-001 | Login session rotation and retained data |
| django-legacy-dep-vuln-001 | Byte decoding, protected values and escaping |
| django-repo-scoped-access-001 | Related-filter choices and visibility |
| django-role-based-access-001 | Widget attribute merge and template rendering |
| django-sensitive-file-exclusion-001 | Connection age and other closure conditions |
| django-template-inherit-recall-001 | Block selection and queue restoration |
| ccx-crossorg-217 | Middleware chain construction and hook order; competing upstream file lists resolved correctly |
| ccx-migration-203 | URL includes, resolvers and callable-view patterns |
| ccx-migration-204 | Model/form maximum length and widget attributes |
| flipt-dep-refactor-001 | Resource identity, revision options and separate context argument |
| flipt-flagexists-refactor-001 | Storage interface and not-found error handling |
| flipt-degraded-context-fix-001 | Boolean and legacy evaluation failure responses |
| flipt-protobuf-metadata-design-001 | Segment matching and response propagation |
| flipt-repo-scoped-access-001 | Batch missing flags, helper dispatch and failures |
| numpy-array-dispatch-refac-001 | Dispatcher wrapping and median dispatcher |
| vscode-custom-fold-region-feat-001 | Collected ranges, typed arrays and storage limits |
| vscode-stale-diagnostics-feat-001 | Overflow markers, owner updates and clearing |
| ccx-onboard-search-213 | Three-way keybinding merge; corrected function ranges |
| ccx-onboard-search-211 | FastICA fitting and unit-variance source computation; upstream description corrected |
| ccx-crossorg-218 | Warning class versus NotFittedError; upstream description corrected |
| ccx-onboard-search-212 | Pivot aggregation, unstacking, filling and totals; corrected function ranges |
| ccx-onboard-search-210 | Pool-ready connection setup and callbacks; corrected function ranges |
| eshop-basket-identity | Identity lookup and missing-identity read/update behavior |
| eshop-basket-storage | Redis keys, JSON storage, missing reads and failed writes |
| eshop-catalog-price-event | Original/new price event and conditional persistence |
| eshop-order-add-product | Existing/new product quantity and discount checks |
| eshop-order-paid | Required prior status, state update and event |
| eshop-order-cancellation | General cancellation restrictions and stock rejection |
| eshop-stock-check-and-removal | Missing catalog products, validation and later stock removal |
| eshop-grace-period | Order selection, database errors and event publication |
| eshop-payment-choice | Configured simulated payment outcome |
| eshop-event-transaction | Commit-before-publication and event failure recording |

## Pending receipts: three tasks

| Task ID | Required correction |
| --- | --- |
| django-modelchoice-fk-fix-001 | R1-F1 |
| django-composite-field-recover-001 | R1-F2 |
| numpy-rolling-median-feat-001 | R1-F3 |

No task was rejected. Preserve task IDs, partition and calibration. Only the 37 accepted task digests may receive accepted receipts in this round.
