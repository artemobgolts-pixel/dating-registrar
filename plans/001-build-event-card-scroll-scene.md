# 001 — Build the event card as one scroll scene

- **Status**: DONE
- **Commit**: ae5d28b
- **Severity**: HIGH
- **Category**: Performance, physicality, spatial consistency
- **Estimated scope**: 3 files, medium-to-large

## Problem

The current scene computes a large global camera translation in
`app/static/landing.css:1061`:

```css
.landing-story {
  --camera-scale: calc(.72 + (var(--story-progress) * .24) - (var(--story-end) * 1.72));
  --camera-y: calc(154px - (var(--story-progress) * 680px) + (var(--story-end) * 2850px));
}
```

It also reserves the complete card while hiding most of its content at
`app/static/landing.css:1407`. The result is an empty sheet at the beginning and
a card that can leave the visible stage later.

## Target

Use one registered GSAP `ScrollTrigger` master timeline with one pinned visual
stage and these labelled phases: photo, surface, essentials, details, float,
focus, swipe, interactive hold, return. Keep the whole card inside
`calc(100svh - header - safe gaps)` at every progress value.

- Scrubbed motion uses `ease: "none"` at the ScrollTrigger level.
- Entrances settle with `power3.out`; on-screen camera moves use `power2.inOut`.
- Animate transform, opacity and a bottom `clip-path` reveal only.
- Float rotation stays within roughly 1–2 degrees and never runs while the user
  is dragging the gallery.
- The automatic swipe is a timeline beat; pointer/keyboard input takes ownership
  immediately and continues from the presented slide.
- Reduced motion renders the complete card statically and preserves all controls.

## Repo conventions to follow

- Existing card interaction cleanup is centralized in
  `app/static/landing-story.js`.
- Existing motion tokens live in `app/static/landing.css`; extend them rather
  than introducing near-duplicates.
- Existing gallery uses Pointer Events and pointer capture; preserve keyboard
  arrows and live-region status.

## Steps

1. Load local GSAP core before local ScrollTrigger and register the plugin before
   initializing the story.
2. Replace scroll-derived CSS variables with a `gsap.context()` master timeline
   pinned to the story stage and refreshed after images/fonts load.
3. Start from the photograph, materialize the glass shell, reveal real card rows,
   add the restrained float, camera focus, automatic swipe, interaction hold and
   symmetric return.
4. Kill/revert the context on Turbo teardown/reinitialization and never stack
   ScrollTriggers.
5. Provide a static reduced-motion path and a no-GSAP progressive enhancement.

## Boundaries

- Do NOT install Lenis or smooth scrolling.
- Do NOT animate width, height, margin, top or left.
- Do NOT move the background or header with GSAP.
- Do NOT let the card leave or clip against the viewport at any breakpoint.

## Verification

- **Mechanical**: run the landing contract tests and JavaScript syntax checks.
- **Feel check**: inspect timeline start/middle/focus/return at desktop, short
  desktop and mobile sizes. At 10% playback, no phase may teleport or expose an
  empty card. Drag during automatic swipe and confirm the card follows the pointer
  immediately. Toggle reduced motion and confirm all content and controls remain.
- **Done when**: the exact user-authored ten-beat sequence is legible, reversible
  by scrolling, interactive after focus, and fully visible throughout.
