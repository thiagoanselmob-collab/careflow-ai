import logging
import os
import re
import json
from typing import List, Optional
from fastapi import APIRouter, Depends, Header, Query, UploadFile, File, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_database import tenant_db_manager
from app.services.chunking import chunk_text
from app.services.embedding import get_embedding

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/knowledge", tags=["RAG Knowledge"])

def get_tenant_id(
    organization_id: Optional[str] = Query(None),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID")
) -> str:
    """
    Extracts the tenant organization ID from query parameters or X-Tenant-ID header.
    """
    tenant_id = organization_id or x_tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="Tenant ID (organization_id query parameter or X-Tenant-ID header) is required."
        )
    return tenant_id

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extracts text contents from raw PDF bytes using stream content search.
    """
    text_parts = []
    # PDF stream matches
    stream_matches = re.findall(b"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.DOTALL)
    for stream in stream_matches:
        # Extract text strings in parentheses
        for match in re.finditer(rb"\(([^)]+)\)", stream):
            try:
                decoded = match.group(1).decode("utf-8", errors="ignore")
                decoded = decoded.replace("\\(", "(").replace("\\)", ")")
                if len(decoded.strip()) > 1:
                    text_parts.append(decoded)
            except Exception:
                pass
    if not text_parts:
        # Fallback: scan whole PDF bytes for parenthesized items
        for match in re.finditer(rb"\(([^)]+)\)", pdf_bytes):
            try:
                decoded = match.group(1).decode("utf-8", errors="ignore")
                decoded = decoded.replace("\\(", "(").replace("\\)", ")")
                if len(decoded.strip()) > 1:
                    text_parts.append(decoded)
            except Exception:
                pass
    return " ".join(text_parts) if text_parts else pdf_bytes.decode("utf-8", errors="ignore")

async def check_embedding_column_exists(session: AsyncSession) -> bool:
    """
    Checks if the embedding column exists in the clinic_knowledge table.
    """
    try:
        await session.execute(text("SELECT embedding FROM clinic_knowledge LIMIT 0;"))
        return True
    except Exception:
        await session.rollback()
        return False

@router.get("")
async def list_knowledge_blocks(tenant_id: str = Depends(get_tenant_id)):
    """
    Lists all active knowledge blocks for a specific tenant.
    """
    try:
        async with await tenant_db_manager.get_tenant_session(tenant_id) as session:
            result = await session.execute(
                text("SELECT id, content, metadata FROM clinic_knowledge ORDER BY id DESC;")
            )
            
            blocks = []
            for row in result.all():
                meta = row[2]
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        pass
                blocks.append({
                    "id": row[0],
                    "content": row[1],
                    "metadata": meta
                })
            return blocks
    except Exception as e:
        logger.error(f"Error listing knowledge blocks for tenant {tenant_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/upload")
async def upload_knowledge_block(
    file: UploadFile = File(...),
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Uploads a file (.txt, .md, .pdf) or manual text blob, chunks it, embeds it, and saves to database.
    """
    try:
        filename = file.filename or "upload.txt"
        ext = os.path.splitext(filename)[1].lower()
        
        content_bytes = await file.read()
        if ext == ".pdf":
            text_content = extract_text_from_pdf(content_bytes)
        else:
            text_content = content_bytes.decode("utf-8", errors="ignore")

        if not text_content.strip():
            raise HTTPException(status_code=400, detail="Document contains no readable text content.")

        # Chunk the text
        chunks = chunk_text(text_content, chunk_size=500, chunk_overlap=50)
        
        async with await tenant_db_manager.get_tenant_session(tenant_id) as session:
            has_vector = await check_embedding_column_exists(session)
            dialect_name = session.bind.dialect.name
            
            for chunk in chunks:
                metadata_dict = {"source": filename, "length": len(chunk)}
                
                # Render metadata field based on database dialect compatibility
                meta_val = metadata_dict if dialect_name == "postgresql" else json.dumps(metadata_dict)
                
                if has_vector:
                    # Calculate vector embedding
                    try:
                        vector = get_embedding(chunk)
                        vector_str = "[" + ",".join(map(str, vector)) + "]"
                    except Exception as e:
                        logger.warning(f"Failed to generate embedding: {e}")
                        vector_str = None

                    if vector_str:
                        await session.execute(
                            text("""
                                INSERT INTO clinic_knowledge (content, metadata, embedding) 
                                VALUES (:content, :metadata, :embedding)
                            """),
                            {
                                "content": chunk,
                                "metadata": meta_val,
                                "embedding": vector_str
                            }
                        )
                    else:
                        await session.execute(
                            text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
                            {
                                "content": chunk,
                                "metadata": meta_val
                            }
                        )
                else:
                    await session.execute(
                        text("INSERT INTO clinic_knowledge (content, metadata) VALUES (:content, :metadata)"),
                        {
                            "content": chunk,
                            "metadata": meta_val
                        }
                    )
            await session.commit()
            
        return {"status": "success", "chunks_created": len(chunks)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading knowledge block: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{id}")
async def delete_knowledge_block(id: int, tenant_id: str = Depends(get_tenant_id)):
    """
    Deletes a knowledge block by ID.
    """
    try:
        async with await tenant_db_manager.get_tenant_session(tenant_id) as session:
            # Check if block exists
            check_res = await session.execute(
                text("SELECT id FROM clinic_knowledge WHERE id = :id"),
                {"id": id}
            )
            if not check_res.scalar_one_or_none():
                raise HTTPException(status_code=404, detail=f"Knowledge block with ID {id} not found.")

            await session.execute(
                text("DELETE FROM clinic_knowledge WHERE id = :id"),
                {"id": id}
            )
            await session.commit()
            return {"status": "success", "message": f"Block {id} deleted successfully."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting knowledge block {id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
