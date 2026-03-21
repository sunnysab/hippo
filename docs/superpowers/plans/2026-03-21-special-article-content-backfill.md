# Special Article Content Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-off backfill script that re-fetches special-type WeChat articles with `content_json = []` and rewrites article content through the current parsing pipeline.

**Architecture:** Keep the one-off entrypoint in `scripts/`, but move the candidate selection and batch orchestration into a small Python module so it can be unit-tested. Reuse `ArticleDownloader` for fetch + parse + persistence, with a thin public method for the backfill path so the script does not reach into downloader internals.

**Tech Stack:** Python, `argparse`, `unittest`, PostgreSQL, existing `MPClient` / `ArticleDownloader`

---

### Task 1: Capture Planning Artifacts

**Files:**
- Create: `docs/superpowers/specs/2026-03-21-special-article-content-backfill-design.md`
- Create: `docs/superpowers/plans/2026-03-21-special-article-content-backfill.md`

- [ ] **Step 1: Confirm the approved spec is committed**
- [ ] **Step 2: Save this implementation plan**
- [ ] **Step 3: Commit the plan document**

```bash
git add docs/superpowers/plans/2026-03-21-special-article-content-backfill.md
git commit -m "docs: add special article content backfill plan"
```

### Task 2: Add Failing Backfill Orchestration Tests

**Files:**
- Create: `tests/test_special_article_backfill.py`
- Test: `tests/test_special_article_backfill.py`

- [ ] **Step 1: Write a failing dry-run test for candidate batching**

```python
def test_dry_run_reports_candidates_without_triggering_downloads(self) -> None:
    outputs: list[str] = []
    stats = asyncio.run(
        run_special_article_backfill(
            storage=fake_storage,
            downloader=fake_downloader,
            dry_run=True,
            batch_size=2,
            limit=3,
            echo=outputs.append,
        )
    )
    self.assertEqual(stats.scanned, 3)
    self.assertEqual(fake_downloader.redownload_calls, [])
```

- [ ] **Step 2: Run `uv run python -m unittest tests.test_special_article_backfill -v` and confirm failure**
- [ ] **Step 3: Add a failing success-path test that expects one candidate to be re-downloaded and counted as updated**
- [ ] **Step 4: Add a failing failure-path test that expects one bad article to be reported and later candidates to continue**

### Task 3: Expose A Public Downloader Entry Point For Content Re-fetch

**Files:**
- Modify: `hippo/downloader.py`
- Test: `tests/test_special_article_backfill.py`

- [ ] **Step 1: Add a thin public async method on `ArticleDownloader` for backfill refresh**

```python
async def redownload_article_content(self, article: ArticleRecord) -> DownloadResult:
    return await self._download_with_retry(
        article,
        with_images=False,
        record_images_only=True,
    )
```

- [ ] **Step 2: Keep the method behavior intentionally narrow**
It must fetch HTML again, parse again, rewrite article content, record image rows, and avoid downloading binary images.

- [ ] **Step 3: Re-run `uv run python -m unittest tests.test_special_article_backfill -v` and confirm the tests still fail on the missing backfill module**

### Task 4: Implement The Backfill Module

**Files:**
- Create: `hippo/special_article_backfill.py`
- Modify: `tests/test_special_article_backfill.py`

- [ ] **Step 1: Add a focused candidate row mapper and batch query helper**

```python
SELECT a.id, a.biz, a.article_id, a.title, a.item_show_type, a.author,
       a.digest, a.cover, a.link, a.source_url, a.publish_at, a.raw_json
FROM articles a
JOIN article_content c ON c.article_pk = a.id
WHERE a.item_show_type = ANY(%s)
  AND c.content_json = '[]'::jsonb
  AND a.id > %s
ORDER BY a.id ASC
LIMIT %s
```

- [ ] **Step 2: Implement `run_special_article_backfill(...)` with batch scanning, dry-run behavior, success/failure accounting, and `start_id` / `limit` handling**
- [ ] **Step 3: Emit explicit progress lines with scanned count, updated count, failed count, and last processed article ID**
- [ ] **Step 4: Ensure the runner only calls `downloader.redownload_article_content(article)` after a candidate is fully mapped**
- [ ] **Step 5: Re-run `uv run python -m unittest tests.test_special_article_backfill -v` and make the new tests pass**

### Task 5: Add The One-Off Script Entry Point

**Files:**
- Create: `scripts/backfill_special_article_content.py`
- Modify: `hippo/special_article_backfill.py`
- Test: `tests/test_special_article_backfill.py`

- [ ] **Step 1: Add an `argparse` script that accepts `--pg-dsn`, `--start-id`, `--limit`, `--batch-size`, `--dry-run`, `--types`, `--worker-prefix`, `--worker-proxy`, and `--workers`**
- [ ] **Step 2: Build the runtime with `PostgresStorage` and `build_downloader_container(..., enable_images=False, enable_image_worker=False)`**
- [ ] **Step 3: Parse the `--types` CSV into a validated integer tuple, defaulting to `5,7,8,10`**
- [ ] **Step 4: Call the backfill runner and return a non-zero exit code only when setup fails, not when individual articles fail**
- [ ] **Step 5: Run `uv run python -m unittest tests.test_special_article_backfill -v` again to confirm the script wiring did not break module-level behavior**

### Task 6: Verify With Targeted Commands

**Files:**
- Test: `tests/test_special_article_backfill.py`
- Test: `scripts/backfill_special_article_content.py`
- Test: `hippo/special_article_backfill.py`
- Test: `hippo/downloader.py`

- [ ] **Step 1: Run `uv run python -m unittest tests.test_special_article_backfill -v`**
Expected: PASS

- [ ] **Step 2: Run a dry-run against the real database**

```bash
set -a; source .env; set +a
uv run python scripts/backfill_special_article_content.py --dry-run --limit 20
```

Expected: Prints only candidates in `item_show_type IN (5,7,8,10)` with `content_json = []`, shows progress, and does not write data.

- [ ] **Step 3: Run a small real batch before full execution**

```bash
set -a; source .env; set +a
uv run python scripts/backfill_special_article_content.py --limit 20 --batch-size 10
```

Expected: Updates successful rows, reports failures without aborting the whole run, and prints the last processed article ID.

- [ ] **Step 4: Verify the target count drops**

```bash
set -a; source .env; set +a
psql "$HIPPO_PG_DSN" -At -c "
SELECT COUNT(*)
FROM articles a
JOIN article_content c ON c.article_pk = a.id
WHERE a.item_show_type IN (5,7,8,10)
  AND c.content_json = '[]'::jsonb;
"
```

Expected: Lower than the pre-backfill baseline for the processed sample.

### Task 7: Review Diff And Commit Implementation

**Files:**
- Create: `hippo/special_article_backfill.py`
- Create: `scripts/backfill_special_article_content.py`
- Create: `tests/test_special_article_backfill.py`
- Modify: `hippo/downloader.py`

- [ ] **Step 1: Review `git diff --stat` and ensure the change set stays limited to the backfill module, script, test, and downloader entry point**
- [ ] **Step 2: Commit the implementation**

```bash
git add hippo/downloader.py hippo/special_article_backfill.py scripts/backfill_special_article_content.py tests/test_special_article_backfill.py
git commit -m "feat: add special article content backfill script"
```
