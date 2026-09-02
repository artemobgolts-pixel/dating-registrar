# Animation plans

| # | Plan | Severity | Status |
|---|---|---|---|
| 001 | [Build the event card as one scroll scene](001-build-event-card-scroll-scene.md) | HIGH | DONE |
| 002 | [Slow the landing appearance wave without geometry shifts](002-stabilize-landing-appearance-wave.md) | MEDIUM | DONE |
| 003 | [Align the landing content with the product](003-align-landing-content.md) | MEDIUM | DONE |

Recommended order: 003 establishes the final DOM contract, 001 animates that contract,
then 002 verifies that appearance changes do not disturb it. All plans are stamped at
commit `ae5d28b`.
