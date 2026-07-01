import asyncio
import sys
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Add app path to sys.path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from app.services.chunking import chunk_text, RecursiveCharacterTextSplitter
from app.services.agents.graph import buscar_conhecimento
from app.core.tenant_database import tenant_db_manager, _init_tenant_db
from app.api.knowledge import extract_text_from_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_chunking_tests():
    print("--- Running Chunking Edge Case Tests ---")
    
    # 1. Empty string
    res = chunk_text("", chunk_size=100, chunk_overlap=10)
    print(f"Empty string: {res} (Expected: [])")
    assert res == [], f"Expected [], got {res}"

    # 2. Spaces only
    res = chunk_text("    ", chunk_size=100, chunk_overlap=10)
    print(f"Spaces only: {res} (Expected: [])")
    assert res == [], f"Expected [], got {res}"

    # 3. Very long word without separators
    long_word = "a" * 1000
    res = chunk_text(long_word, chunk_size=100, chunk_overlap=10)
    print(f"Long word count: {len(res)}, lengths: {[len(c) for c in res]}")
    # The default RecursiveCharacterTextSplitter splits by separators: ["\n\n", "\n", " ", ""].
    # When no separators exist, it falls back to empty separator "" (character-by-character split).
    # Let's check if the chunks are <= chunk_size.
    for i, c in enumerate(res):
        assert len(c) <= 100, f"Chunk {i} exceeded 100: {len(c)}"
    print("Long word without separators handled correctly.")

    # 4. Special characters
    special_text = "🌟 Emojis 🌟 and non-ASCII characters: áéíóúçñ. Check if length function handles characters correctly."
    res = chunk_text(special_text, chunk_size=30, chunk_overlap=5)
    print(f"Special characters chunks: {res}")
    for c in res:
        assert len(c) <= 30, f"Chunk exceeded 30: {len(c)}"
        
    # 5. Massive text (1MB)
    huge_text = "This is a sentence. " * 50000  # ~1MB
    import time
    start = time.perf_counter()
    res = chunk_text(huge_text, chunk_size=500, chunk_overlap=50)
    duration = time.perf_counter() - start
    print(f"Massive text split into {len(res)} chunks in {duration:.4f} seconds.")
    assert len(res) > 0

async def test_pgvector_fallback():
    print("\n--- Running pgvector Fallback Tests ---")
    # Setup SQLite in-memory engine to simulate lack of pgvector
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    await _init_tenant_db(engine)
    
    tenant_db_manager._engines["test_org_fallback"] = engine
    tenant_db_manager._sessionmakers["test_org_fallback"] = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    
    # Insert mock clinic knowledge
    async with tenant_db_manager._sessionmakers["test_org_fallback"]() as session:
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "A clínica aceita convênios como Unimed e Amil.", "metadata": '{"source": "rules.txt"}'}
        )
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "Dr. André Seabra atende segunda-feira.", "metadata": '{"source": "rules.txt"}'}
        )
        await session.commit()

    # Test full text search
    print("Testing buscar_conhecimento with exact match fallback...")
    results = await buscar_conhecimento("convênios", "test_org_fallback", limit=2)
    print(f"Query 'convênios' results: {results}")
    assert len(results) == 1
    assert "Unimed" in results[0]["content"]

    # Test word-by-word fallback
    print("Testing buscar_conhecimento with word-by-word fallback...")
    # "convênios inexistentes" -> "convênios" matches but "inexistentes" does not. Since they are OR'ed, it should find Unimed.
    results = await buscar_conhecimento("convênios inexistentes", "test_org_fallback", limit=2)
    print(f"Query 'convênios inexistentes' results: {results}")
    assert len(results) == 1
    assert "Unimed" in results[0]["content"]

    # Clean up
    await engine.dispose()
    tenant_db_manager._engines.pop("test_org_fallback", None)
    tenant_db_manager._sessionmakers.pop("test_org_fallback", None)
    print("pgvector fallback tests passed successfully.")

def test_pdf_extraction_edge_cases():
    print("\n--- Running PDF Extraction Edge Cases ---")
    # Empty bytes
    empty_pdf = b""
    res = extract_text_from_pdf(empty_pdf)
    print(f"Empty PDF bytes result: {res!r}")
    assert res == ""

    # Bytes with no stream structure but parenthesized text
    no_stream = b"Some random content with (parenthesized text) and other stuff."
    res = extract_text_from_pdf(no_stream)
    print(f"No stream PDF result: {res!r}")
    assert "parenthesized text" in res

    # Bad UTF-8 encoded bytes in parentheses
    bad_utf8 = b"Some content (\\xff\\xfe\\xfd) other text."
    res = extract_text_from_pdf(bad_utf8)
    print(f"Bad UTF-8 PDF result: {res!r}")

if __name__ == "__main__":
    asyncio.run(run_chunking_tests())
    asyncio.run(test_pgvector_fallback())
    test_pdf_extraction_edge_cases()
    print("\nALL VERIFICATIONS PASSED!")
