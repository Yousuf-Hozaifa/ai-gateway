from gateway.metrics import RollingStats, percentile
from gateway.security import RateLimiter, key_id, key_matches


def test_key_matching():
    assert key_matches("secret", ("other", "secret"))
    assert not key_matches("nope", ("secret",))
    assert not key_matches("", ("secret",))
    assert not key_matches("ключ", ("secret",))  # non-ASCII must not raise


def test_key_id_is_short_and_stable():
    assert key_id("secret") == key_id("secret") and len(key_id("secret")) == 8 and "secret" not in key_id("secret")


def test_rate_limiter_bucket_and_refill():
    now = [0.0]
    limiter = RateLimiter(60, clock=lambda: now[0])  # 1 token per second, burst of 60
    assert all(limiter.allow("a") for _ in range(60))
    assert not limiter.allow("a")
    assert limiter.allow("b")  # separate client
    now[0] += 2.0
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")


def test_rate_limit_zero_disables():
    limiter = RateLimiter(0)
    assert all(limiter.allow("a") for _ in range(1000))


def test_percentile_and_rolling_stats():
    assert percentile([], 0.5) == 0.0
    assert percentile(list(range(1, 101)), 0.95) == 95
    stats = RollingStats(size=10)
    for i in range(20):
        stats.record("/x", float(i), 500 if i == 19 else 200)
    snap = stats.snapshot()["/x"]
    assert snap["count"] == 10 and snap["error_rate"] == 0.1
