"""retry tests"""

import asyncio
import dataclasses
import datetime
from typing import Iterator
from unittest import mock

import pytest
from wandb.sdk.lib import retry


@dataclasses.dataclass
class MockTime:
    now: datetime.datetime
    sleep: mock.Mock
    sleep_async: mock.Mock


@pytest.fixture(autouse=True)
def mock_time() -> Iterator[MockTime]:
    """Mock out the now()/sleep() funcs used by the retry logic."""
    now = datetime.datetime.now()

    def _sleep(seconds):
        nonlocal now
        now += datetime.timedelta(seconds=seconds)

    async def _sleep_async(seconds):
        nonlocal now
        now += datetime.timedelta(seconds=seconds)
        await asyncio.sleep(1e-9)  # let the event loop shuffle stuff around

    with mock.patch(
        "wandb.sdk.lib.retry.NOW_FN",
        wraps=lambda: now,
    ) as mock_now, mock.patch(
        "wandb.sdk.lib.retry.SLEEP_FN", side_effect=_sleep
    ) as mock_sleep, mock.patch(
        "wandb.sdk.lib.retry.SLEEP_ASYNC_FN", side_effect=_sleep_async
    ) as mock_sleep_async:
        yield MockTime(now=mock_now, sleep=mock_sleep, sleep_async=mock_sleep_async)


def test_retry_respects_num_retries():
    func = mock.Mock()
    func.side_effect = ValueError

    num_retries = 7
    retrier = retry.Retry(
        func,
        num_retries=num_retries,
        retryable_exceptions=(ValueError,),
    )
    with pytest.raises(ValueError):
        retrier()

    assert func.call_count == num_retries + 1


def test_retry_call_num_retries_overrides_default_num_retries():
    func = mock.Mock()
    func.side_effect = ValueError

    retrier = retry.Retry(
        func,
        retryable_exceptions=(ValueError,),
    )
    num_retries = 4
    with pytest.raises(ValueError):
        retrier(num_retries=num_retries)

    assert func.call_count == num_retries + 1


def test_retry_respects_num_retries_across_multiple_calls():
    func = mock.Mock()
    func.side_effect = ValueError

    num_retries = 7
    retrier = retry.Retry(
        func,
        num_retries=num_retries,
        retryable_exceptions=(ValueError,),
    )
    with pytest.raises(ValueError):
        retrier()
    with pytest.raises(ValueError):
        retrier()

    assert func.call_count == 2 * (num_retries + 1)


def test_retry_respects_retryable_exceptions():
    func = mock.Mock()
    func.side_effect = ValueError

    retrier = retry.Retry(
        func,
        retryable_exceptions=(ValueError,),
        num_retries=3,
    )
    with pytest.raises(ValueError):
        retrier()

    assert func.call_count > 1

    func.reset_mock()
    func.side_effect = IndexError
    retrier = retry.Retry(
        func,
        retryable_exceptions=(ValueError,),
    )
    with pytest.raises(IndexError):
        retrier()

    assert func.call_count == 1


def test_retry_respects_secondary_timeout(mock_time: MockTime):
    func = mock.Mock()
    func.side_effect = ValueError

    t0 = mock_time.now()

    def check_retry_timeout(e):
        if isinstance(e, ValueError):
            return datetime.timedelta(minutes=10)

    retry_timedelta = datetime.timedelta(hours=7)
    retrier = retry.Retry(
        func,
        retryable_exceptions=(ValueError,),
        check_retry_fn=check_retry_timeout,
        retry_timedelta=retry_timedelta,
        num_retries=10000,
    )
    with pytest.raises(ValueError):
        retrier()

    # add some slop for other timeout calls, should be about 10 minutes of retries
    assert 10 <= (mock_time.now() - t0).total_seconds() / 60 < 20


class MyError(Exception):
    pass


SECOND = datetime.timedelta(seconds=1)


class TestAsync:
    class TestFilteredBackoff:
        def test_reraises_exc_failing_predicate(self):
            wrapped = mock.Mock(spec=retry.Backoff)
            filtered = retry.FilteredBackoff(
                filter=lambda e: False,
                wrapped=wrapped,
            )

            with pytest.raises(MyError):
                filtered.next_sleep_or_reraise(MyError("don't retry me"))

            wrapped.next_sleep_or_reraise.assert_not_called()

        def test_delegates_exc_passing_predicate(self):
            retriable_exc = MyError("retry me")
            wrapped = mock.Mock(
                spec=retry.Backoff,
                next_sleep_or_reraise=mock.Mock(return_value=123 * SECOND),
            )
            filtered = retry.FilteredBackoff(
                filter=lambda e: e == retriable_exc,
                wrapped=wrapped,
            )

            assert filtered.next_sleep_or_reraise(retriable_exc) == 123 * SECOND
            wrapped.next_sleep_or_reraise.assert_called_once_with(retriable_exc)

    class TestRetryLoopLoggingBackoff:
        def test_respects_max_retries(self, mock_time: MockTime):
            events = []
            backoff = retry.RetryLoopLoggingBackoff(
                wrapped=mock.MagicMock(spec=retry.Backoff),
                on_loop_start=lambda e: events.append(("start", e)),
                on_loop_end=lambda dt: events.append(("end", dt)),
            )

            excs = [MyError(str(i)) for i in range(3)]

            with backoff:
                backoff.next_sleep_or_reraise(excs[0])
                mock_time.sleep(1.0)
                backoff.next_sleep_or_reraise(excs[1])
                mock_time.sleep(1.0)
                backoff.next_sleep_or_reraise(excs[2])
                mock_time.sleep(1.0)

            assert events == [("start", excs[0]), ("end", 3 * SECOND)]

    class TestExponentialBackoff:
        def test_respects_max_retries(self):
            backoff = retry.ExponentialBackoff(
                initial_sleep=SECOND, max_sleep=SECOND, max_retries=3
            )
            for _ in range(3):
                backoff.next_sleep_or_reraise(MyError())
            with pytest.raises(MyError):
                backoff.next_sleep_or_reraise(MyError())

        def test_respects_timeout(self, mock_time: MockTime):
            t0 = mock_time.now()
            dt = 300 * SECOND
            backoff = retry.ExponentialBackoff(
                initial_sleep=SECOND, max_sleep=10 * dt, timeout_at=t0 + dt
            )
            with pytest.raises(MyError):
                for _ in range(9999):
                    mock_time.sleep(
                        backoff.next_sleep_or_reraise(MyError()).total_seconds()
                    )

            assert t0 + dt <= mock_time.now() <= t0 + 2 * dt

    class TestRetryAsync:
        def test_follows_backoff_schedule(self, mock_time: MockTime):
            fn = mock.Mock(side_effect=MyError("oh no"))
            with pytest.raises(MyError):
                asyncio.run(
                    retry.retry_async(
                        mock.MagicMock(
                            spec=retry.Backoff,
                            next_sleep_or_reraise=mock.Mock(
                                side_effect=[
                                    1 * SECOND,
                                    2 * SECOND,
                                    MyError(),
                                ]
                            ),
                        ),
                        fn,
                        "pos1",
                        "pos2",
                        kw1="kw1",
                        kw2="kw2",
                    )
                )

            mock_time.sleep_async.assert_has_calls(
                [
                    mock.call(1.0),
                    mock.call(2.0),
                ]
            )

            fn.assert_has_calls(
                [
                    mock.call("pos1", "pos2", kw1="kw1", kw2="kw2"),
                    mock.call("pos1", "pos2", kw1="kw1", kw2="kw2"),
                    mock.call("pos1", "pos2", kw1="kw1", kw2="kw2"),
                ]
            )

        def test_uses_backoff_context_manager(self, mock_time: MockTime):
            backoff = mock.MagicMock(
                spec=retry.Backoff,
                next_sleep_or_reraise=mock.Mock(return_value=1 * SECOND),
            )

            async def _fn():
                assert backoff.__enter__.call_count == 1
                assert backoff.__exit__.call_count == 0

            fn = mock.AsyncMock(
                wraps=_fn,
                side_effect=[
                    Exception("transient error"),
                    Exception("transient error"),
                    mock.DEFAULT,
                ],
            )

            asyncio.run(retry.retry_async(backoff, fn))

            assert fn.call_count == 3
            assert backoff.__enter__.call_count == 1
            assert backoff.__exit__.call_count == 1
