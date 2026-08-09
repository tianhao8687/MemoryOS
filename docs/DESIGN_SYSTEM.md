# MemoryOS management UI design system

The management UI follows three generated reference screens stored beside this file in
`docs/design/`. They define the implementation baseline for Overview, Conflicts, and Candidates.

## Visual direction

- Canvas: cool light gray; never cream or warm beige.
- Navigation: graphite rail with a restrained teal active edge.
- State colors: teal for active/verified, amber for needs-review/conflict, red only for destructive
  actions or failures.
- Containers: open tables, rails, grouped lists, and inspectors; avoid generic card grids.
- Corners: 6-10px radius; hairline borders; nearly shadowless.
- Typography: humanist UI sans paired with monospace for IDs, hashes, timestamps, and provenance.
- Motion: short state transitions only; always honor `prefers-reduced-motion`.

## Required component families

App shell, sidebar navigation, repository/branch bar, search, data table, timeline rail, inspector,
status label, conflict comparison, confirm/reject controls, filters, empty/error/loading states, and
responsive mobile navigation.

## Reference and implementation evidence

Generated concept references:

- `docs/design/overview-reference.png`
- `docs/design/conflicts-reference.png`
- `docs/design/candidates-reference.png`

Native application captures:

- `docs/verification/ui-desktop-1536x1024.png`
- `docs/verification/ui-conflicts-1536x1024.png`
- `docs/verification/ui-candidates-1536x1024.png`
- `docs/verification/ui-mobile-412x915.png`
- `docs/verification/ui-candidates-mobile-412x915.png`

The implementation preserves the reference hierarchy, graphite navigation, cool-gray canvas,
teal active state, amber conflict state, dense ledger/table treatment, and right-side inspector.
Reference screens contained richer illustrative counts and shortcut copy; the implementation uses
real fixture/API counts and omits shortcuts that are not implemented. Mobile replaces the fixed
rail and inspector with bounded drawers so the document never exceeds the viewport width.

## Accessibility and responsive behavior

- Semantic headings, tables, forms, labels, dialogs, complementary inspectors, live status/error
  messages, and keyboard-accessible links/buttons.
- Focus-visible outlines and a skip link.
- Color is paired with text labels rather than used as the only status signal.
- Motion is disabled under `prefers-reduced-motion`.
- Playwright runs desktop Chromium at 1536x1024 and mobile Chromium using the Pixel 7 profile;
  axe runs on the desktop overview and browser console/page errors fail every E2E case.
- Manual in-app-browser QA additionally measured the 412px mobile document, body, and candidate
  drawer widths and confirmed no horizontal overflow.
