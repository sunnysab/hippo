# Article Reader Facets Design

**Date:** 2026-03-20

**Scope**

- Enlarge the article preview account avatar in the reader header.
- Keep the article preview account name on a single line with truncation.
- Collapse article type facet chips by default and allow explicit expand/collapse.

**Context**

The article reader header currently compresses identity metadata too aggressively. The avatar is visually undersized, and long account names wrap into multiple short lines because the adjacent metadata row competes for horizontal space. The article type facet chips also consume too much vertical space when many types are available.

**Goals**

- Increase identity clarity in the reader header.
- Preserve a stable one-line account name in the preview header.
- Reduce initial filter height without hiding the active type selection.

**Non-goals**

- No redesign of the article filter form structure.
- No replacement of type facets with a dropdown or drawer.
- No new frontend dependency or bundling step.

**Design**

## Reader Header Layout

Increase `.article-preview-avatar` to a more legible size and prevent it from shrinking inside the flex row. Give `.article-preview-account` enough width priority so the account block remains readable before secondary metadata starts wrapping. Force `.article-preview-name` to a single line with ellipsis overflow to keep the header compact and predictable.

## Type Facet Collapse

Render article type facets in a collapsed state by default. In collapsed mode:

- Show `All Types` first.
- Show a small fixed number of facet chips after it.
- If the active type would otherwise be hidden, include it in the visible set.
- Append a toggle chip that expands to reveal all types.

In expanded mode, show all facet chips plus a collapse chip. The toggle must expose `aria-expanded` and use i18n strings.

## Interaction Rules

- Collapse state is local UI state and does not affect filtering semantics.
- Clicking a facet still updates the existing select filter and triggers the current reload flow.
- If the facet count does not exceed the collapsed limit, do not render the toggle chip.

## Testing

- Add a frontend logic regression test for collapsed facet selection, including active-item preservation.
- Add CSS regression assertions for the avatar size and single-line account name behavior.
