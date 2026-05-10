# Sync Failure Banner in Header

## Problem

When a sync task fails (e.g. login expired, network error), users have no obvious indication on
the main page — the failure is only visible inside the Settings > Sync page. Users need a
prominent banner below `header.topbar` showing the failure reason and a quick way to jump to
the login page (`/#/settings/login`).

## Design

### Overview

Extend the existing `.banner` element in `AppShell.tsx` to also trigger on sync failure,
using the `last_error` field from `/api/settings/status`. Style errors more prominently
than the existing login-required warning.

### Changes

**1. `frontend/src/components/AppShell.tsx`**

In `refreshChromeMeta`, after fetching `/api/settings/status`:
- Read the `last_error` field from the response
- If `last_error` is non-empty, set `bannerText` to the error string and `bannerVisible = true`
- Sync failure takes priority over login status: if `last_error` exists, use it; otherwise
  fall back to the existing login status check
- Add a `bannerError` state flag to distinguish sync error from login warning for styling

**2. `frontend/src/styles/common.css`**

Add `.banner.is-error` modifier class using danger colors:
- Background: `var(--danger-soft)` (pinkish red)
- Text: `var(--danger)` (dark red)
- This makes sync failures visually more urgent than the warning-gold login banner

**3. `frontend/src/i18n/zh-CN.json`**

No new i18n keys needed: the error text comes directly from the backend's `last_error`
field (already Chinese). The button label `login.relogin` already exists.

### Data Flow

```
GET /api/settings/status → { last_error: "..." }
  ↓
AppShell.refreshChromeMeta()
  ↓
if last_error → bannerText = last_error, bannerVisible = true, bannerError = true
  ↓
render <div className="banner is-error">...</div>
```

### Styling

Two banner variants:
| Variant | Trigger | CSS class | Background | Text |
|---|---|---|---|---|
| Warning | Login required | `.banner` | `--warning-soft` gold | `--warning` dark gold |
| Error | Sync failed | `.banner.is-error` | `--danger-soft` pink | `--danger` dark red |

### Testing

- Manual: Cause a sync failure, reload frontend, verify banner appears below topbar
- Manual: Verify "Re-login" button navigates to `/#/settings/login`
- Manual: Verify banner disappears after sync succeeds (last_error cleared)
