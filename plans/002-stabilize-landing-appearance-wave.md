# 002 — Slow the landing appearance wave without geometry shifts

- **Status**: DONE
- **Commit**: ae5d28b
- **Severity**: MEDIUM
- **Category**: Interruptibility, cohesion
- **Estimated scope**: 3 files, small-to-medium

## Problem

`app/static/theme.js:25` uses a 320–480 ms skin wave globally, while
`app/static/landing-story.js:656` hides one complete demo card and shows another.
The root View Transition therefore snapshots different card geometries and can
visibly jerk the pinned scene.

## Target

- Labels are `Классика` and `Романтика`.
- On the landing only, skin transitions use a visible 1000 ms circular reveal;
  light/dark uses 650 ms.
- One card shell remains at identical geometry; only content and palette change.
- Rapid repeated input interrupts from the current state; controls never lock.
- Reduced motion applies the state immediately or through a short opacity-only
  transition.

## Repo conventions to follow

Continue using `document.startViewTransition`, `d4y:skinchange`,
`d4y:themechange`, and the existing cookie behavior in `app/static/theme.js`.

## Steps

1. Add landing-specific timing data/attributes without slowing other product pages.
2. Convert the demo to one geometry-stable shell whose text and media sources are
   switched in place.
3. Preserve the current interruption generation guard and pointer-event safety.
4. Test rapid mixed skin/theme clicks and card bounds before/after each transition.

## Boundaries

- Do NOT change stored cookie values (`friends`, `romantic`).
- Do NOT slow theme controls outside the landing.
- Do NOT animate layout properties.

## Verification

- **Mechanical**: run theme data tests and landing tests.
- **Feel check**: click each appearance control repeatedly at 10% playback. The
  wave must remain obvious, the card frame must not shift by a pixel, and the
  final state must equal the last click.
- **Done when**: the landing wave is atmospheric but the rest of the product
  remains responsive.
