# 003 — Align the landing content with the product

- **Status**: DONE
- **Commit**: ae5d28b
- **Severity**: MEDIUM
- **Category**: Purpose, cohesion
- **Estimated scope**: 4 templates/styles plus image assets and tests

## Problem

The hero in `app/static/landing.css:947` uses `min-height: 100svh` with
`align-items: flex-end`, creating a blank first screen. The demo card diverges
from the public card: it adds `Событие`, a standalone capacity row, separate map
actions, large participant tiles and a custom voting section. Feature previews
still use gradient placeholders, and the settings preview contains switches and
the non-owner setting `Права гостей`.

## Target

- Compact hero below the fixed header; smaller statement and card.
- Remove the header link `О сервисе`.
- Match the public event anatomy: media/payment badge, title, date/calendar,
  linked place, description, external links, compact count/progress/participant
  chips, `Выбрать` and `Спросить` actions.
- Remove the translucent stage panel behind the event card.
- Use real image assets for all six currently empty visual slots.
- Render the exact approved settings lists as labelled dot rows, not toggles.

## Repo conventions to follow

Use `app/templates/public/category.html`, `app/templates/public/share.html` and
`app/static/public.css` as the source of truth for terminology and visual anatomy.

## Steps

1. Compact the hero while preserving header/footer design and dynamic ink background.
2. Refactor the demo markup to the real public-card structure and one shell.
3. Replace placeholder covers/avatar with project-local WebP images.
4. Replace the settings toggles with the exact approved collection/event rows.
5. Update contract tests for wording, anatomy, image references and accessibility.

## Boundaries

- Do NOT redesign the header or footer.
- Do NOT change product database/domain behavior.
- Do NOT add `Права гостей` or unapproved settings.
- Do NOT embed text or logos in generated photos.

## Verification

- **Mechanical**: template contract tests, accessibility tests and image existence
  checks pass.
- **Feel check**: first viewport contains meaningful hero content, the stage has no
  panel behind the card, and each image crop works in light/dark and both skins.
- **Done when**: copy, imagery and card anatomy describe the product rather than a
  generic questionnaire.
