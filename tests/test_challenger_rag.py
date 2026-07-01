import pytest
import pytest_asyncio
import asyncio
from io import BytesIO
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.services.chunking import chunk_text, RecursiveCharacterTextSplitter
from app.services.agents.graph import buscar_conhecimento
from app.api.knowledge import extract_text_from_pdf
from app.core.tenant_database import tenant_db_manager, _init_tenant_db

@pytest_asyncio.fixture
async def challenger_tenant_db():
    """
    Sets up an in-memory SQLite database for testing, simulating
    a database without pgvector support.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    await _init_tenant_db(engine)
    
    tenant_db_manager._engines["challenger_org"] = engine
    tenant_db_manager._sessionmakers["challenger_org"] = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    
    yield engine
    
    await engine.dispose()
    tenant_db_manager._engines.pop("challenger_org", None)
    tenant_db_manager._sessionmakers.pop("challenger_org", None)


@pytest.mark.asyncio
async def test_chunking_edge_cases():
    """
    Verify chunking behavior under edge cases:
    - Empty string
    - Whitespace only
    - Extremely long word (no space) exceeding chunk_size
    - Special characters and unicode
    """
    # 1. Empty string
    assert chunk_text("") == []
    assert chunk_text("   ") == []

    # 2. Extremely long word (no spaces)
    long_word = "A" * 1000
    chunks = chunk_text(long_word, chunk_size=100, chunk_overlap=10)
    # The custom RecursiveCharacterTextSplitter splits by character if no separator is found
    # and splits recursive block into chunks <= chunk_size.
    assert len(chunks) > 0
    for c in chunks:
        assert len(c) <= 100

    # 3. Unicode and special characters
    special_text = "😊" * 200 + " 🌟 " + "🔥" * 200
    chunks_special = chunk_text(special_text, chunk_size=100, chunk_overlap=10)
    assert len(chunks_special) > 0
    for c in chunks_special:
        assert len(c) <= 100
        assert "😊" in c or "🌟" in c or "🔥" in c


@pytest.mark.asyncio
async def test_pdf_extraction_edge_cases():
    """
    Verify extract_text_from_pdf behavior:
    - Empty bytes
    - Stream with invalid UTF-8 bytes
    - Stream with nested/unmatched parentheses
    - Multiple streams
    """
    # 1. Empty bytes
    assert extract_text_from_pdf(b"") == ""

    # 2. Standard stream match
    pdf_data = b"stream\n(Hello World)\nendstream"
    assert extract_text_from_pdf(pdf_data) == "Hello World"

    # 3. Invalid UTF-8 bytes in parentheses
    pdf_invalid_utf8 = b"stream\n(" + b"\xff\xfeHello" + b")\nendstream"
    # Should ignore or decode with ignore
    extracted = extract_text_from_pdf(pdf_invalid_utf8)
    assert "Hello" in extracted

    # 4. Unmatched parentheses inside stream
    # re.finditer(rb"\(([^)]+)\)", stream) will match up to the first closing parenthesis.
    pdf_nested = b"stream\n(Nested (Parenthesis) here)\nendstream"
    extracted_nested = extract_text_from_pdf(pdf_nested)
    # The regex matches "Nested (Parenthesis" as group 1 since [^)]+ doesn't allow ')'
    # Wait, let's verify what actually gets extracted.
    assert "Nested (Parenthesis" in extracted_nested

    # 5. Missing endstream fallback
    pdf_no_endstream = b"stream\n(Isolated text)"
    # Fallback matches all parenthesized bytes in the document
    assert extract_text_from_pdf(pdf_no_endstream) == "Isolated text"


@pytest.mark.asyncio
async def test_buscar_conhecimento_fallback_and_query_variations(challenger_tenant_db):
    """
    Verify fallback ILIKE/text search when pgvector is unavailable.
    - Check exact match.
    - Check case-insensitive match (ILIKE/LIKE behavior).
    - Check word-by-word fallback matching when the full query has no match.
    - Check empty/whitespace query behavior.
    """
    sessionmaker = tenant_db_manager._sessionmakers["challenger_org"]
    async with sessionmaker() as session:
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "A clínica de reprogramação metabólica do Dr. André Seabra está localizada em São Paulo.", "metadata": '{"source": "loc.txt"}'}
        )
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "Aceitamos convênios Bradesco, Amil, SulAmérica.", "metadata": '{"source": "insurance.txt"}'}
        )
        await session.commit()

    # 1. Fallback exact match
    res = await buscar_conhecimento("convênios", "challenger_org", limit=2)
    assert len(res) == 1
    assert "insurance.txt" in res[0]["metadata"]
    assert res[0]["similarity"] == 0.5  # Hardcoded fallback similarity for direct match

    # 2. Fallback case-insensitive match (SQLite is case-insensitive for LIKE on ASCII, but for non-ASCII it depends on configuration. Let's check.)
    res_case = await buscar_conhecimento("CONVÊNIOS", "challenger_org", limit=2)
    # In SQLite, LIKE is case-insensitive by default for ASCII characters.
    # For non-ASCII like "Ê", SQLite's standard LIKE might be case-sensitive. Let's check "bradesco" vs "BRADESCO".
    res_ascii = await buscar_conhecimento("BRADESCO", "challenger_org", limit=2)
    assert len(res_ascii) == 1

    # 3. Word-by-word fallback
    # If we search "onde fica a clínica de reprogramação metabólica?", the direct match might fail if
    # the entire query string is not a substring.
    # Direct substring of "onde fica a clínica de reprogramação metabólica?" isn't in content.
    # But word-by-word fallback will split and search for words like "clínica", "reprogramação", "metabólica".
    res_word = await buscar_conhecimento("onde fica a clínica de reprogramação metabólica?", "challenger_org", limit=2)
    assert len(res_word) == 1
    assert "loc.txt" in res_word[0]["metadata"]
    assert res_word[0]["similarity"] == 0.4  # Hardcoded fallback similarity for word-by-word

    # 4. Empty/whitespace query behaviour
    # If query is empty, direct LIKE %""% will match everything.
    # Let's test what happens with an empty query:
    res_empty = await buscar_conhecimento("", "challenger_org", limit=10)
    # The LIKE "%" query matches all rows. Let's verify:
    assert len(res_empty) == 2  # Both rows returned


class MockResult:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_buscar_conhecimento_pgvector_no_fallback_on_zero_results(monkeypatch):
    """
    Verify that if pgvector search is active and runs successfully, but returns 0 results
    (i.e. similarities are all < 0.70), it does NOT fall back to textual LIKE/ILIKE search.
    """
    executed_queries = []

    # Mock get_embedding
    monkeypatch.setattr("app.services.embedding.get_embedding", lambda x: [0.1] * 768)

    # A mock Session class that intercepts executes
    class MockAsyncSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def rollback(self):
            pass
        async def execute(self, sql_text, params=None):
            query_str = str(sql_text)
            executed_queries.append((query_str, params))
            if "SELECT embedding FROM clinic_knowledge" in query_str:
                # pgvector is available
                return MockResult([])
            elif "1.0 - (embedding <=> :query_embedding)" in query_str:
                # pgvector search executes successfully but returns 0 results
                return MockResult([])
            elif "content LIKE" in query_str or "content ILIKE" in query_str:
                # Text fallback query (should NOT be called!)
                return MockResult([(99, "fallback text content", '{"source": "mock.txt"}')])
            return MockResult([])

    # Mock tenant sessionmaker to yield our mock session
    async def mock_get_tenant_session(org_id):
        return MockAsyncSession()

    monkeypatch.setattr(tenant_db_manager, "get_tenant_session", mock_get_tenant_session)

    # Execute search
    results = await buscar_conhecimento("test query", "challenger_org", limit=3)

    # Verify that results are empty (pgvector returned 0, no fallback occurred)
    assert len(results) == 0

    # Verify which queries were run
    queries = [q[0] for q in executed_queries]
    # Check that pgvector availability check was run
    assert any("SELECT embedding FROM clinic_knowledge" in q for q in queries)
    # Check that pgvector search query was run
    assert any("1.0 - (embedding <=> :query_embedding)" in q for q in queries)
    # Critical assert: text fallback query was NOT run!
    assert not any("content LIKE" in q or "content ILIKE" in q for q in queries)


@pytest.mark.asyncio
async def test_chunking_performance_stress():
    """
    Measure chunking performance under a heavy contiguous load (no separators).
    """
    import time
    # 50KB string with absolutely no spaces or newlines
    massive_contiguous_string = "X" * 50000
    
    start_time = time.perf_counter()
    chunks = chunk_text(massive_contiguous_string, chunk_size=500, chunk_overlap=50)
    duration = time.perf_counter() - start_time
    
    # Verify correctness
    assert len(chunks) > 0
    for c in chunks:
        assert len(c) <= 500
        
    print(f"\n[BENCHMARK] Chunking 50KB contiguous string took {duration:.4f} seconds.")
    # It should complete in a reasonable timeframe (e.g. less than 0.5 seconds)
    assert duration < 0.5, f"Chunking is too slow: took {duration:.4f}s"
