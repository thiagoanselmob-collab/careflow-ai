import pytest
from unittest.mock import MagicMock, AsyncMock
from app.services.embedding import get_embedding, aget_embedding, get_embedding_model

def test_get_embedding_empty_text():
    assert get_embedding("") == []
    assert get_embedding(None) == []

@pytest.mark.asyncio
async def test_aget_embedding_empty_text():
    assert await aget_embedding("") == []
    assert await aget_embedding(None) == []

def test_get_embedding_success(monkeypatch):
    mock_model = MagicMock()
    mock_model.embed_query.return_value = [0.1, 0.2, 0.3]
    
    monkeypatch.setattr("app.services.embedding.get_embedding_model", lambda: mock_model)
    
    res = get_embedding("hello world")
    assert res == [0.1, 0.2, 0.3]
    mock_model.embed_query.assert_called_once_with("hello world")

@pytest.mark.asyncio
async def test_aget_embedding_success(monkeypatch):
    mock_model = MagicMock()
    # async embed query should be a coroutine
    async def mock_aembed(text):
        return [0.4, 0.5, 0.6]
    mock_model.aembed_query = mock_aembed
    
    monkeypatch.setattr("app.services.embedding.get_embedding_model", lambda: mock_model)
    
    res = await aget_embedding("hello world async")
    assert res == [0.4, 0.5, 0.6]

def test_get_embedding_error(monkeypatch):
    mock_model = MagicMock()
    mock_model.embed_query.side_effect = Exception("API error")
    
    monkeypatch.setattr("app.services.embedding.get_embedding_model", lambda: mock_model)
    
    with pytest.raises(Exception) as exc:
        get_embedding("error test")
    assert "API error" in str(exc.value)

@pytest.mark.asyncio
async def test_aget_embedding_error(monkeypatch):
    mock_model = MagicMock()
    async def mock_aembed(text):
        raise Exception("Async API error")
    mock_model.aembed_query = mock_aembed
    
    monkeypatch.setattr("app.services.embedding.get_embedding_model", lambda: mock_model)
    
    with pytest.raises(Exception) as exc:
        await aget_embedding("error test async")
    assert "Async API error" in str(exc.value)
