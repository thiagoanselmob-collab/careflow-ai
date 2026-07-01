import pytest
import httpx
from unittest import mock
import os
from scripts.simulate_load import send_webhook, verify_database, main

@pytest.mark.asyncio
async def test_send_webhook_timeout():
    """
    Test send_webhook when httpx client raises a TimeoutException.
    It should handle it gracefully, print an error, and return -1.0.
    """
    async def mock_post(*args, **kwargs):
        raise httpx.TimeoutException("Connection timed out")

    mock_client = mock.MagicMock(spec=httpx.AsyncClient)
    mock_client.post = mock_post

    latency = await send_webhook(mock_client, "http://localhost:8000", "test_tenant", "12345", "hello")
    assert latency == -1.0


@pytest.mark.asyncio
async def test_send_webhook_http_error():
    """
    Test send_webhook when the server returns a non-200 status code.
    It should print an error and return -1.0.
    """
    mock_response = mock.MagicMock(spec=httpx.Response)
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    async def mock_post(*args, **kwargs):
        return mock_response

    mock_client = mock.MagicMock(spec=httpx.AsyncClient)
    mock_client.post = mock_post

    latency = await send_webhook(mock_client, "http://localhost:8000", "test_tenant", "12345", "hello")
    assert latency == -1.0


@pytest.mark.asyncio
async def test_verify_database_exception_handling(monkeypatch):
    """
    Test verify_database when database connections raise exceptions.
    It should propagate the exception so the caller main function can catch it.
    """
    # Mock settings query to raise an exception
    mock_session = mock.MagicMock()
    mock_session.execute = mock.AsyncMock(side_effect=Exception("Central DB Connection Refused"))

    class FakeSessionmaker:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("scripts.simulate_load.async_sessionmaker", lambda *args, **kwargs: FakeSessionmaker)
    monkeypatch.setattr("scripts.simulate_load.settings", mock.MagicMock(database_url="sqlite+aiosqlite:///:memory:"))

    with pytest.raises(Exception, match="Central DB Connection Refused"):
        await verify_database("wh_tenant", ["+5511990000001"])


@pytest.mark.asyncio
async def test_main_with_violations_and_db_failure(monkeypatch):
    """
    Test main function when there are SLA violations or database verification failures.
    It should exit with 1.
    """
    # Mock parse_args
    class MockArgs:
        url = "http://localhost:8000"
        tenant = "test_tenant"
        phones = 2
        messages = 1
        debounce_wait = 1

    monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda self: MockArgs())

    # Mock run_load to return high latencies (violating SLA > 500ms)
    async def mock_run_load(*args, **kwargs):
        return [0.6, 0.7], ["+5511990000001", "+5511990000002"]
    
    monkeypatch.setattr("scripts.simulate_load.run_load", mock_run_load)

    # Mock verify_database to raise Exception (simulating DB failure/no-connection)
    async def mock_verify_database(*args, **kwargs):
        raise Exception("Database offline")
    
    monkeypatch.setattr("scripts.simulate_load.verify_database", mock_verify_database)

    # Mock sys.exit to verify it gets called with 1
    exit_mock = mock.MagicMock()
    monkeypatch.setattr("sys.exit", exit_mock)

    # We also mock asyncio.sleep to run instantly
    monkeypatch.setattr("asyncio.sleep", mock.AsyncMock())

    await main()

    # The script should try to exit with code 1 due to SLA violation and DB failure
    exit_mock.assert_called_once_with(1)
