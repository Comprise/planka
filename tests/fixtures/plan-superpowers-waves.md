<!-- Образец корпуса: задачи в форме skills/writing-plans/SKILL.md плагина superpowers 6.4.2 («Task Structure»), сгруппированные в волны заголовками `## Wave N`, как в plan-waves.md; содержимое сгенерировано. Пункт «(read-only)» — файл, который задача читает; в волне 2 настоящий конфликт `README.md`. -->
# Rate Limiter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Limit API requests per client with a token bucket and report the limit in response headers.

**Architecture:** A `limiter` package holds the bucket and an in-memory store; a middleware applies it to every route. Configuration is read from the existing `Settings` object.

**Tech Stack:** Python 3.12, Starlette, pytest.

## Global Constraints

- Limits come from `Settings.rate_limit` only; no hard-coded numbers.
- Headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`.

---

## Wave 1

### Task 1: Token bucket

**Files:**
- Create: `src/api/limiter/bucket.py`
- Test: `tests/limiter/test_bucket.py`
- `src/api/config.py` (read-only)

**Interfaces:**
- Consumes: `Settings.rate_limit: RateLimit` from `src/api/config.py`.
- Produces: `TokenBucket(capacity: int, refill_per_s: float).take(now: float) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
def test_bucket_refills():
    b = TokenBucket(1, 1.0)
    assert b.take(0.0) and not b.take(0.5) and b.take(1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/limiter/test_bucket.py -v`
Expected: FAIL with "No module named 'api.limiter'"

- [ ] **Step 3: Implement `TokenBucket` in `src/api/limiter/bucket.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

### Task 2: Bucket store

**Files:**
- Create: `src/api/limiter/store.py`
- Test: `tests/limiter/test_store.py`
- `src/api/config.py` (read-only)

**Interfaces:**
- Consumes: `Settings.rate_limit` from `src/api/config.py`.
- Produces: `BucketStore.get(client_id: str) -> TokenBucket`.

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `BucketStore` in `src/api/limiter/store.py`**
- [ ] **Step 4: Commit**

## Wave 2

### Task 3: Middleware

**Files:**
- Create: `src/api/limiter/middleware.py`
- Modify: `src/api/app.py:12-30`
- Test: `tests/limiter/test_middleware.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Implement `RateLimitMiddleware` in `src/api/limiter/middleware.py`**
- [ ] **Step 3: Commit**

### Task 4: Settings documentation

**Files:**
- Modify: `docs/settings.md`
- Modify: `README.md`

- [ ] **Step 1: Document `rate_limit` in `docs/settings.md`**
- [ ] **Step 2: Commit**

### Task 5: Response headers documentation

**Files:**
- Modify: `docs/headers.md`
- Modify: `README.md`

- [ ] **Step 1: Document the headers in `docs/headers.md`**
- [ ] **Step 2: Commit**
