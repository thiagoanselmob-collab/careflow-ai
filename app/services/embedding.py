import logging
from typing import List
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from app.core.config import settings

logger = logging.getLogger(__name__)

def get_embedding_model() -> GoogleGenerativeAIEmbeddings:
    """
    Returns the GoogleGenerativeAIEmbeddings model instance initialized with
    the GEMINI_API_KEY from application settings.
    """
    return GoogleGenerativeAIEmbeddings(
        model="models/text-embedding-004",
        google_api_key=settings.gemini_api_key
    )

def get_embedding(text: str) -> List[float]:
    """
    Generates a 768-dimensional embedding vector for the provided text.
    """
    if not text:
        return []
    try:
        model = get_embedding_model()
        return model.embed_query(text)
    except Exception as e:
        logger.error(f"Error generating embedding: {e}")
        raise e

async def aget_embedding(text: str) -> List[float]:
    """
    Asynchronously generates a 768-dimensional embedding vector for the provided text.
    """
    if not text:
        return []
    try:
        model = get_embedding_model()
        return await model.aembed_query(text)
    except Exception as e:
        logger.error(f"Error generating embedding asynchronously: {e}")
        raise e
