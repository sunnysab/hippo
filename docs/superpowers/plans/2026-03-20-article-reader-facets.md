# Article Reader Facets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve article reader header readability and reduce article type facet footprint with a default-collapsed chip row.

**Architecture:** Keep the existing server-rendered page and vanilla JS flow. Extract facet-collapse calculation into a small standalone browser helper that can be tested with `quickjs`, then consume it from `static/articles.js` while adjusting `static/articles.css` for the visual layout fixes.

**Tech Stack:** Vanilla JavaScript, static CSS, Python `unittest`, `quickjs`

---

### Task 1: Document The Change

**Files:**
- Create: `docs/superpowers/specs/2026-03-20-article-reader-facets-design.md`
- Create: `docs/superpowers/plans/2026-03-20-article-reader-facets.md`

- [ ] **Step 1: Save the approved design**
- [ ] **Step 2: Save the implementation plan**
- [ ] **Step 3: Commit the documentation**

### Task 2: Add Failing Frontend Regression Tests

**Files:**
- Create: `tests/test_articles_frontend_logic.py`
- Test: `tests/test_articles_frontend_logic.py`

- [ ] **Step 1: Write a failing test for collapsed facet visibility**
- [ ] **Step 2: Run `uv run python -m unittest tests.test_articles_frontend_logic -v` and confirm failure**
- [ ] **Step 3: Add CSS assertions for avatar sizing and account-name truncation**

### Task 3: Implement Facet Collapse And Header Layout

**Files:**
- Create: `static/articles_facets.js`
- Modify: `static/index.html`
- Modify: `static/articles.js`
- Modify: `static/articles.css`
- Modify: `static/i18n/zh-CN.json`

- [ ] **Step 1: Add a pure helper for facet visibility calculation**
- [ ] **Step 2: Integrate the helper into article facet rendering**
- [ ] **Step 3: Add the expand/collapse facet toggle with i18n labels**
- [ ] **Step 4: Adjust reader header sizing and truncation styles**
- [ ] **Step 5: Run the targeted frontend regression test**

### Task 4: Verify And Commit

**Files:**
- Test: `tests/test_articles_frontend_logic.py`
- Test: `static/articles.js`
- Test: `static/articles.css`

- [ ] **Step 1: Run `uv run python -m unittest tests.test_articles_frontend_logic -v`**
- [ ] **Step 2: Review `git diff --stat`**
- [ ] **Step 3: Commit the implementation with a conventional commit message**
