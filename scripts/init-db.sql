-- Enable vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create settings table
CREATE TABLE IF NOT EXISTS settings (
    organization_id VARCHAR(255) PRIMARY KEY,
    tenant_connection_string TEXT NOT NULL
);

-- Pre-create the basic tables: clinic_knowledge, message_buffer, dados_cliente
CREATE TABLE IF NOT EXISTS clinic_knowledge (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    metadata JSONB,
    embedding VECTOR(768)
);

CREATE INDEX IF NOT EXISTS clinic_knowledge_embedding_idx 
ON clinic_knowledge USING hnsw(embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS message_buffer (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(50) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dados_cliente (
    phone_number VARCHAR(50) PRIMARY KEY,
    status VARCHAR(50) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
