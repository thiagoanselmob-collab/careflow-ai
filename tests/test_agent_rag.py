import pytest
import pytest_asyncio
from io import BytesIO
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.main import app
from app.core.tenant_database import tenant_db_manager, _init_tenant_db
from app.services.chunking import chunk_text, RecursiveCharacterTextSplitter
from app.services.embedding import get_embedding_model, get_embedding
from app.services.agents.graph import buscar_conhecimento, rag_node

client = TestClient(app)

@pytest.fixture
def chunker():
    return RecursiveCharacterTextSplitter(chunk_size=100, chunk_overlap=10)

def test_recursive_splitter(chunker):
    """
    Verify the custom RecursiveCharacterTextSplitter splits text and respects overlap.
    """
    text_to_split = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua."
    chunks = chunker.split_text(text_to_split)
    assert len(chunks) > 1
    # Check that none of the chunks exceed the chunk size
    for chunk in chunks:
        assert len(chunk) <= 100

def test_chunk_text_helper():
    """
    Verify chunk_text helper splits correctly.
    """
    text_to_split = "This is a simple sentence that we want to split into smaller pieces for our RAG knowledge base. Let's see how it behaves."
    chunks = chunk_text(text_to_split, chunk_size=50, chunk_overlap=5)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 50

def test_embedding_model():
    """
    Verify get_embedding_model returns expected instance.
    """
    model = get_embedding_model()
    assert model.model == "models/text-embedding-004"

@pytest_asyncio.fixture
async def mock_tenant_db():
    # Set up in-memory tenant database
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    await _init_tenant_db(engine)
    
    # Register mock tenant in connection manager cache
    tenant_db_manager._engines["test_org"] = engine
    tenant_db_manager._sessionmakers["test_org"] = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession
    )
    
    yield engine
    
    # Teardown
    await engine.dispose()
    tenant_db_manager._engines.pop("test_org", None)
    tenant_db_manager._sessionmakers.pop("test_org", None)

@pytest.mark.asyncio
async def test_buscar_conhecimento_fallback(mock_tenant_db):
    """
    Verify fallback ILIKE/LIKE search in buscar_conhecimento works when pgvector is unavailable.
    """
    # Insert mock knowledge
    sessionmaker = tenant_db_manager._sessionmakers["test_org"]
    async with sessionmaker() as session:
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "O Dr. André Seabra atende nas segundas e terças feiras.", "metadata": '{"source": "rules.txt"}'}
        )
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "A clínica aceita convênios como Unimed e Amil.", "metadata": '{"source": "rules.txt"}'}
        )
        await session.commit()
        
    # Search for "convênios"
    results = await buscar_conhecimento("convênios", "test_org", limit=2)
    assert len(results) == 1
    assert "Unimed" in results[0]["content"]

@pytest.mark.asyncio
async def test_knowledge_api_endpoints(mock_tenant_db):
    """
    Verify API endpoints: POST, GET, DELETE.
    """
    # 1. POST /api/v1/admin/knowledge/upload
    # Simulate file upload
    file_content = b"Regras da clinica: Atendimento das 8h as 18h."
    file_io = BytesIO(file_content)
    
    response = client.post(
        "/api/v1/admin/knowledge/upload?organization_id=test_org",
        files={"file": ("rules.txt", file_io, "text/plain")}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # 2. GET /api/v1/admin/knowledge
    response = client.get("/api/v1/admin/knowledge?organization_id=test_org")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "rules.txt" in data[0]["metadata"]["source"]
    assert "Atendimento" in data[0]["content"]
    
    # Get ID
    block_id = data[0]["id"]
    
    # 3. DELETE /api/v1/admin/knowledge/{id}
    response = client.delete(f"/api/v1/admin/knowledge/{block_id}?organization_id=test_org")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Verify deleted
    response = client.get("/api/v1/admin/knowledge?organization_id=test_org")
    assert len(response.json()) == 0

@pytest.mark.asyncio
async def test_rag_node_execution(mock_tenant_db):
    """
    Verify rag_node returns expected structure and routes correctly.
    """
    # Insert some mock knowledge
    sessionmaker = tenant_db_manager._sessionmakers["test_org"]
    async with sessionmaker() as session:
        await session.execute(
            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
            {"content": "A clinica aceita convenio Bradesco Saude.", "metadata": '{"source": "rules.txt"}'}
        )
        await session.commit()

    from app.schemas.session import MessageSchema, CollectedDataSchema
    from app.services.agents.graph import AgentState
    
    state: AgentState = {
        "messages": [
            MessageSchema(role="user", content="Quais convênios a clínica aceita?")
        ],
        "bot_active": True,
        "collected_data": CollectedDataSchema(),
        "wants_to_schedule": False,
        "next_node": None,
        "action_required": None
    }
    
    # We pass a MockLLM class that simulates standard chat responses
    class MockChatResponse:
        def __init__(self, content):
            self.content = content
            
    class MockChatLLM:
        async def ainvoke(self, messages, *args, **kwargs):
            return MockChatResponse("A clínica aceita o convênio Bradesco Saúde.")
            
    config = {
        "configurable": {
            "tenant_id": "test_org",
            "llm": MockChatLLM()
        }
    }
    
    result = rag_node(state, config=config)
    assert len(result["messages"]) == 1
    assert "Bradesco" in result["messages"][0].content
    assert result["next_node"] is None
