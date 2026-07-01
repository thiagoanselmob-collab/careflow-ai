# Original User Request

## Initial Request — 2026-06-29T00:50:28Z

Dynamic Multi-tenant PostgreSQL Connectors: Implement connection configuration for Medflow central database, a Python equivalent AES-256-GCM encryption/decryption service using the `cryptography` library, and a Tenant Connection Manager querying the `tenant_connection_string` column in the `settings` table and caching async connection pools per organization ID.

Working directory: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend`
Integrity mode: development

## Requirements

### R1. Medflow Central Database Configuration
In `app/core/database.py`, configure the asynchronous connection using the `DATABASE_URL` env variable. The central database contains a `settings` table storing encrypted connection credentials for each tenant/organization.

### R2. AES-256-GCM Decryption (Medflow Java Equivalent)
Implement a Python encryption/decryption service in `app/services/encryption.py` that mirrors Java's `EncryptionService.java` using the `cryptography` library:
- **Dependencies**: Add `cryptography` to `pyproject.toml`.
- **Algorithm**: AES-256-GCM.
- **Key Derivation**: PBKDF2 with HMAC-SHA256, using salt `"MedFlowCRM-EncryptionSalt-2024"`, 600,000 iterations, producing a 256-bit key.
- **Ciphertext format**: Input is a Base64-encoded string which when decoded yields `IV (12 bytes) + Ciphertext (including GCM tag)`.
- **Encryption Key**: Loaded from environment variable `ENCRYPTION_KEY`.

### R3. Dynamic Tenant Connection Manager (`TenantConnectionManager`)
Implement `app/core/tenant_database.py`:
- Query the `tenant_connection_string` column from the `settings` table in the central database using `organization_id`.
- Decrypt the connection string using the decryption service.
- Maintain an active cache of async connection pools (using `asyncpg` and SQLAlchemy async engine) per tenant (`organization_id`).
- Ensure complete pool isolation and cleanup (closing all active pools) upon application shutdown.

## Acceptance Criteria

### Test Validation
- [ ] Implement `tests/test_encryption.py` containing unit tests asserting successful decryption of values encrypted under the Medflow Java rules.
- [ ] Implement `tests/test_tenant_database.py` verifying dynamic pool creation, session caching, isolation, and shutdown cleanups, using mocked/in-memory databases where appropriate (no live Postgres required).
- [ ] Running `poetry run pytest` succeeds and all tests pass with 100% success.

## Follow-up — 2026-06-29T04:51:47Z

Redis Session Management: Implement patient session state schemas using Pydantic, and an asynchronous Redis Session Manager using `redis.asyncio` to store, update, retrieve, and clear tenant-segregated patient sessions (keyed by `{organization_id}:{phone_number}`) with a 24-hour expiration (TTL).

Working directory: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend`
Integrity mode: development

## Requirements

### R1. Pydantic Session Schemas
Create Pydantic schemas in `app/schemas/session.py` to structure patient session state:
- **MessageSchema**: Contains `role` (either `"user"` or `"assistant"`), `content` (string), and `timestamp` (datetime).
- **CollectedDataSchema**: Contains fields for `full_name` (optional string), `cpf` (optional string), `grievance` (optional string), `preferred_doctor` (optional string), and `selected_datetime` (optional datetime).
- **SessionSchema**: Contains `messages_history` (list of `MessageSchema`), `bot_active` (boolean, default `True`), `collected_data` (an instance of `CollectedDataSchema`), and `last_message_at` (optional datetime).

### R2. Asynchronous Redis Session Manager
Implement `app/services/session_manager.py` using `redis.asyncio`:
- **Redis Connection**: Load `redis_url` from config Settings. Initialize an asynchronous Redis client utilizing a connection pool.
- **Segregated Keys**: Store and retrieve sessions under the composite key format `{organization_id}:{phone_number}`.
- **Lifecyle Methods**:
  - `async def get_session(organization_id: str, phone_number: str) -> Optional[SessionSchema]`
  - `async def update_session(organization_id: str, phone_number: str, session_data: SessionSchema) -> None`
  - `async def clear_session(organization_id: str, phone_number: str) -> None`
- **TTL**: Enforce a 24-hour expiration time on every session write.
- **Resilience**: Defensively catch Redis connection/offline errors and raise controlled exceptions.

### R3. Dependencies
- Add `fakeredis` to the development dependencies group in `pyproject.toml`.

## Acceptance Criteria

### Test Validation
- [ ] Implement `tests/test_session_manager.py` to verify full session lifecycle (get, update, TTL write, clear, separation by composite key) using `fakeredis`.
- [ ] Verify that Redis offline errors are handled gracefully and raise custom exceptions.
- [ ] Running `poetry run pytest` succeeds and all tests pass with 100% success.

## Follow-up — 2026-06-29T15:07:28Z

Substituir o stub `crc_sdr_node` por um agente SDR completo usando Claude (via `ChatAnthropic`) com arquitetura multi-tenant de personas. O sistema prompt do SDR deve ser **configurável por perfil de cliente** (tenant), não hardcoded para um médico específico. O Dr. André Seabra é o primeiro perfil implementado como referência, mas a arquitetura deve suportar novos perfis futuros sem alteração de código. Inclui coleta progressiva de dados, contorno de objeções e extração estruturada via Pydantic.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

---

## Context

### Existing Codebase (Phase 3.1 — já entregue)

- **`app/services/agents/graph.py`** — LangGraph `StateGraph` compilado com:
  - `AgentState` (TypedDict): `messages`, `bot_active`, `collected_data`, `next_node`, `action_required`
  - `supervisor_node`: Roteador central com Gemini (ChatGoogleGenerativeAI) + `RoutingDecision` schema
  - `crc_sdr_node`: **STUB** — este é o nó a ser substituído pela implementação real
  - `agenda_node`, `rag_node`: stubs funcionais (não alterar)
  - Hub-and-spoke: `supervisor → node → supervisor → END`
  - `session_to_agent_state()` / `agent_state_to_session()`: conversores bidirecionais
- **`app/schemas/session.py`** — `SessionSchema`, `MessageSchema`, `CollectedDataSchema` (fields: `full_name`, `cpf`, `grievance`, `preferred_doctor`, `selected_datetime`)
- **`tests/test_agent_graph.py`** — 6 testes existentes usando `MockLLM` (passando com supervisor mock). Não alterar estes testes.
- **`pyproject.toml`** — Poetry com `langgraph`, `langchain`, `langchain-google-genai` já instalados. Falta `langchain-anthropic`.
- **`docs/agentes_specs.md`** — Especificação completa dos agentes (Seção 2: Agente SDR/CRC). This file contains the Dr. André Seabra persona as a reference but the architecture must be tenant-agnostic.

### CRITICAL DESIGN DECISION: Multi-Tenant Persona Architecture

The CareFlow AI system is **multi-tenant**. Each clinic/doctor is a different client (tenant). The SDR agent's persona, business rules, and objection handling must NOT be hardcoded for any specific doctor. Instead:

1. **The system prompt must be a template** that accepts configurable parameters (clinic name, doctor name, specialization focus, value proposition, objection handling strategy, etc.)
2. **Dr. André Seabra is the FIRST client profile** — used as reference implementation and for testing. His specific rules (metabolic reprogramming, integrative health, no generic diets) are stored as a **profile/config**, not baked into the SDR node code.
3. **Future clients** will be added by creating new profiles with their own persona parameters — zero code changes needed in the SDR node itself.

### SDR Agent Business Rules (generic, applicable to ALL tenants)

These rules are universal across all client profiles:
1. **Premium Positioning:** Position the doctor's expertise and methodology as premium/differentiated. Never make it sound generic.
2. **Price Objection Handling:** Never list prices upfront. First generate value about the doctor's method, then redirect to understanding the patient's pain/goal. Structure: Acknowledge → Differentiate → Redirect.
3. **Progressive Data Collection:** Never ask for CPF in the first interaction. Ask for full name first. Later, justify CPF as necessary for confidential medical records.
4. **History Awareness:** Always check `messages` history before re-asking for data already provided.
5. **Scheduling Transition:** When lead agrees to schedule AND Name+CPF are present, signal `wants_to_schedule=True`.
6. **WhatsApp Style:** Short, humanized messages. Use patient's name once identified. No dense text blocks.

### Dr. André Seabra Profile (first tenant — for testing)

- **Clinic:** Clínica do Dr. André Seabra
- **Specialization:** Reprogramação metabólica, equilíbrio hormonal, emagrecimento definitivo, saúde integrativa
- **Value Proposition:** "Não é dieta de gaveta. É investigação profunda de exames, taxa metabólica e hormônios para um plano de emagrecimento definitivo focado na raiz do problema."
- **Objection Script Example:** "A consulta com o Dr. André Seabra é um programa completo de reprogramação metabólica..."

---

## Requirements

### R1. Dependência Anthropic e Nó SDR
Add `langchain-anthropic` to Poetry dependencies. Replace the `crc_sdr_node` stub with a full SDR agent implementation that uses Claude (via `ChatAnthropic`) with configurable API key (`ANTHROPIC_API_KEY` env var). The node must consume and return `AgentState`, integrating cleanly into the existing LangGraph graph. The LLM must be injectable via `config["configurable"]["sdr_llm"]` for testability (same pattern as the supervisor's `config["configurable"]["llm"]`).

### R2. Multi-Tenant Persona Architecture
The SDR node must accept persona configuration as a parameter (via config, constructor, or a profile dict), NOT hardcode any specific doctor's details. The system prompt must be a **template** with placeholders for:
- Clinic/doctor name
- Specialization/methodology description
- Value proposition text
- Objection handling script

Create a default profile dict/config for Dr. André Seabra as the reference implementation. The generic SDR business rules (progressive collection, price objection pattern, WhatsApp style) are built into the node logic itself, while the tenant-specific details (doctor name, specialization, scripts) come from the profile.

### R3. Structured Output Extraction
The SDR node must use Claude's structured output (`.with_structured_output`) with this Pydantic schema:

```python
class SDROutputSchema(BaseModel):
    response_message: str = Field(..., description="Mensagem de texto a ser enviada ao paciente")
    extracted_name: Optional[str] = Field(default=None, description="Nome completo extraído do diálogo")
    extracted_cpf: Optional[str] = Field(default=None, description="CPF extraído (apenas dígitos)")
    wants_to_schedule: bool = Field(default=False, description="True se o paciente quer agendar consulta")
```

The node must update `collected_data.full_name` and `collected_data.cpf` in the AgentState when extracted. It must NOT overwrite existing values with None.

### R4. Test Suite
Create `tests/test_sdr_node.py` with unit tests using a mocked Claude LLM (no real API calls). Tests must verify:
- Correct routing through the graph (supervisor → crc_sdr_node → supervisor → END)
- `collected_data` is updated when the mock returns extracted name/CPF
- `collected_data` is NOT overwritten when mock returns None for name/CPF
- `wants_to_schedule=True` is correctly propagated
- The existing 53 tests continue to pass (`poetry run pytest`)

---

## Acceptance Criteria

### Dependencies
- [ ] `langchain-anthropic` is in `pyproject.toml` under `[tool.poetry.dependencies]`
- [ ] `poetry install` completes without errors

### SDR Node Integration
- [ ] The `crc_sdr_node` stub in `graph.py` is replaced with the real SDR implementation (or the graph imports from a new module)
- [ ] The LangGraph graph compiles without exceptions (`.compile()` succeeds)
- [ ] The SDR node accepts `AgentState` and returns a valid state update with `messages` containing the response

### Multi-Tenant Architecture
- [ ] The SDR system prompt is built from a template + profile config — not hardcoded for a specific doctor
- [ ] A Dr. André Seabra profile dict/config exists as the default/reference profile
- [ ] Changing the profile config (e.g., swapping doctor name, specialization) does NOT require modifying the SDR node source code

### Structured Extraction
- [ ] `SDROutputSchema` exists with all 4 fields: `response_message`, `extracted_name`, `extracted_cpf`, `wants_to_schedule`
- [ ] When mock LLM returns `extracted_name="João Silva"`, `collected_data.full_name` is updated in the state
- [ ] When mock LLM returns `extracted_name=None`, existing `collected_data.full_name` is preserved (not overwritten)
- [ ] When mock LLM returns `wants_to_schedule=True`, the state reflects this for the supervisor's next routing decision

### Tests
- [ ] `tests/test_sdr_node.py` exists with at least 4 test cases covering the scenarios above
- [ ] All tests use mocked Claude LLM (no real API calls to Anthropic during `pytest`)
- [ ] All 53 existing tests continue to pass (no regressions)
- [ ] `poetry run pytest` passes with 100% success (0 failures, 0 errors)

### Code Quality
- [ ] No circular imports between existing modules and the new SDR code
- [ ] Profile contains clinic-specific references (not generic placeholders in production config)

## Follow-up — 2026-06-29T19:33:34Z

Complete a Fase 3.2 do CareFlow AI que foi parcialmente implementada por um agente anterior que morreu durante a execução. A implementação principal já está pronta, faltam apenas os testes.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

---

## Context — O que já foi implementado (NÃO ALTERAR o código existente)

O agente anterior implementou com sucesso em `app/services/agents/graph.py`:
- `SDROutputSchema` (Pydantic) com 4 campos: `response_message`, `extracted_name`, `extracted_cpf`, `wants_to_schedule`
- `DEFAULT_SDR_PROFILE` dict multi-tenant (Dr. André Seabra como primeiro perfil)
- `crc_sdr_node` completo usando Claude (ChatAnthropic) com:
  - System prompt baseado em template multi-tenant
  - Structured output extraction via `.with_structured_output(SDROutputSchema)`
  - Non-overwrite de `collected_data` (não sobrescreve valores existentes com None)
  - Fallback compatível com MockLLM para testes do supervisor
  - LLM injectável via `config["configurable"]["sdr_llm"]`
- `AgentState` atualizado com `wants_to_schedule: Optional[bool]`
- `SessionSchema` em `app/schemas/session.py` atualizado com `wants_to_schedule: bool = Field(default=False)`
- `langchain-anthropic` adicionado ao `pyproject.toml`
- **54 testes existentes passando** (incluindo os 6 testes originais do supervisor em `tests/test_agent_graph.py`)

## O que FALTA fazer

### R1. Criar `tests/test_sdr_node.py`
Create a comprehensive test file for the SDR node with mocked Claude LLM. The tests must:

1. **Test name extraction**: When mock SDR LLM returns `SDROutputSchema(response_message="Olá!", extracted_name="João Silva", extracted_cpf=None, wants_to_schedule=False)`, verify that `collected_data.full_name` is updated to `"João Silva"` in the final state.

2. **Test non-overwrite**: When mock SDR LLM returns `extracted_name=None` but state already has `collected_data.full_name="Maria Santos"`, verify that `full_name` remains `"Maria Santos"` (not overwritten with None).

3. **Test CPF extraction**: When mock SDR LLM returns `extracted_cpf="12345678900"`, verify `collected_data.cpf` is updated.

4. **Test wants_to_schedule propagation**: When mock SDR LLM returns `wants_to_schedule=True`, verify the state reflects this.

5. **Test full graph routing**: Simulate a user message routed through supervisor → crc_sdr_node → supervisor → END, verifying the complete flow works.

### Mock Pattern
The SDR node checks `config["configurable"]["sdr_llm"]` for the LLM. Create a `MockSDRLLM` class that:
- Has `.with_structured_output(schema)` returning a `MockStructuredSDRLLM`
- The `MockStructuredSDRLLM.invoke(prompt)` returns an `SDROutputSchema` instance with controllable values
- Also provide the existing `MockLLM` for the supervisor via `config["configurable"]["llm"]`

Reference the existing test pattern in `tests/test_agent_graph.py` for how `MockLLM` and `graph_config` work.

---

## Acceptance Criteria

- [ ] `tests/test_sdr_node.py` exists with at least 5 test cases
- [ ] All tests use mocked LLMs (no real API calls to Claude or Gemini)
- [ ] All 54 existing tests continue to pass (no regressions)
- [ ] `poetry run pytest` passes with 100% success (0 failures, 0 errors)
- [ ] Tests verify `collected_data` update AND non-overwrite behavior

## Follow-up — 2026-06-29T16:53:57-03:00

Implementar a Fase 3.3 do CareFlow AI: Agente de Agendamento - Regras de Escassez e Calendário. Substituir o stub `agenda_node` por um agente real usando Gemini (ChatGoogleGenerativeAI), criar o cliente HTTP `MedflowClient` para integração com o backend Java do Medflow, e adicionar regras de escassez (2 slots com gap de 20+ dias) e validação cadastral.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

---

## Context

### Existing Codebase (Phase 3.2 — já entregue)

- **`app/services/agents/graph.py`** — Grafo LangGraph completo com:
  - `AgentState`: `messages`, `bot_active`, `collected_data`, `wants_to_schedule`, `next_node`, `action_required`
  - `supervisor_node`: Roteador central baseado em LLM
  - `crc_sdr_node`: Agente SDR completo usando Claude (ChatAnthropic) com multi-tenant persona template e extração estruturada
  - `agenda_node`: Stub a ser substituído nesta fase
- **`app/schemas/session.py`** — Schemas de sessão e dados coletados (`full_name`, `cpf`, etc.)
- **`app/core/config.py`** — `Settings` class carregando configurações globais.
- **`tests/test_agent_graph.py`** & **`tests/test_sdr_node.py`** — 60 testes passando com 100% de sucesso

---

## Requirements

### R1. Configuração e Cliente HTTP Medflow (app/services/medflow_client.py)
1. **Configurações**: Adicionar as propriedades `medflow_api_url: str = Field(default="http://localhost:8080")` e `medflow_jwt_token: str = Field(default="mock_token")` ao schema `Settings` em `app/core/config.py`.
2. **Cliente HTTP**: Criar o cliente assíncrono `MedflowClient` utilizando `httpx.AsyncClient` para interagir com o backend Medflow de acordo com `docs/medflow_api_contracts.md`:
   - `get_crm_appointments(date: str, doctor_id: str) -> List[dict]`: GET `/api/appointments/crm`
   - `update_appointment_status(appointment_id: str, status: str) -> dict`: PATCH `/api/appointments/{id}/status`
   - `book_appointment(...) -> dict`: POST `/api/webhooks/n8n/book-appointment`
   - `confirm_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/confirm-appointment/{appointmentId}`
   - `cancel_appointment(appointment_id: str) -> dict`: POST `/api/webhooks/n8n/cancel-appointment/{appointmentId}`
3. **Segurança e Idempotência**: Passar o token JWT no cabeçalho `Authorization: Bearer <token>` e tratar chaves de idempotência via cabeçalho `Idempotency-Key` em mutações (POST/PATCH).

### R2. Lógica do Nó da Agenda (agenda_node)
Substitua a lógica de stub `agenda_node` em `graph.py` (ou em módulo separado importado):
- O LLM Gemini (`ChatGoogleGenerativeAI`) deve ser injetável via `config["configurable"]["agenda_llm"]` para testes.
- **Time Anchor**: Injete a data/hora atual do sistema (Timezone `America/Sao_Paulo`) no prompt para o modelo traduzir datas relativas ("amanhã", "sexta que vem") em absolutas (`YYYY-MM-DD`).

### R3. Regras Administrativas e Escassez (2 Slots)
O nó da agenda deve impor no prompt/sistema:
1. **Bloqueio Cadastral**: Se `full_name` ou `cpf` em `collected_data` estiverem faltando, o nó deve gerar uma mensagem educada solicitando as informações em falta e redefinir `next_node` para `None`/`END` para encerrar o turno, aguardando que o usuário forneça os dados (que serão processados pelo supervisor no próximo turno).
2. **Confirmação Explícita**: Nunca registrar um agendamento (`book`) ou confirmar (`confirm`) sem consentimento expresso do paciente.
3. **Escassez Obrigatória (2 Slots)**: Ao propor horários livres para agendamento (após verificar a agenda do médico), propor **exatamente dois slots**:
   - **Slot 1 (Opção Próxima)**: O horário livre mais próximo no calendário (hoje ou amanhã).
   - **Slot 2 (Opção Escassa)**: O horário livre mais próximo a partir de pelo menos **20 dias no futuro** da data atual.
   - **Fallback**: Se não houver horários exatos nessas janelas, sugerir os dois horários livres disponíveis mais próximos dessas faixas.

### R4. Chamada de Ferramentas / Saída Estruturada
O `agenda_node` deve usar saída estruturada (`.with_structured_output`) com o Gemini para decidir a ação de agendamento e retornar a resposta textual:
- Pydantic schema com campos contendo a resposta ao usuário, a ação a ser tomada (search, book, confirm, cancel, ou text_only) e parâmetros de agendamento se aplicável (data, hora, etc.).

### R5. Suíte de Testes
Criar `tests/test_agent_agenda.py` que:
- Simule o cliente `MedflowClient` via `httpx.MockTransport` (ou mocks apropriados) para cenários offline.
- Verifique recusa por falta de Nome/CPF.
- Verifique cálculo correto de datas relativas com Time Anchor.
- Verifique a técnica de escassez (2 slots com gap de 20+ dias ou fallback).
- Garanta que todos os 60 testes anteriores e os novos testes passem com 100% de sucesso.

---

## Acceptance Criteria

### Configuration & Client
- [ ] `Settings` in `app/core/config.py` contains `medflow_api_url` and `medflow_jwt_token`.
- [ ] `MedflowClient` exists in `app/services/medflow_client.py` using `httpx.AsyncClient`.
- [ ] `MedflowClient` methods use correct REST headers (`Authorization: Bearer <token>` and `Idempotency-Key`).

### Agenda Node Integration
- [ ] The `agenda_node` stub in `app/services/agents/graph.py` is replaced with the real implementation.
- [ ] The graph compiles successfully (`graph = workflow.compile()`).
- [ ] `agenda_node` checks `config["configurable"]["agenda_llm"]` for LLM dependency injection.

### Business Rules & Scarcity
- [ ] If `full_name` or `cpf` is missing, the node generates a request message and terminates the turn (sets `next_node` to `None`/`END`).
- [ ] Suggests exactly 2 scheduling slots (Slot 1: today/tomorrow; Slot 2: 20+ days out) or explains fallback.
- [ ] Uses Timezone `America/Sao_Paulo` for time anchor calculations in system prompt.

### Tests & Quality
- [ ] `tests/test_agent_agenda.py` exists with at least 5 test cases.
- [ ] Tests verify date resolution, scarcity slot generation, blocking on missing demographics, and error paths (e.g. 409 Conflict).
- [ ] All unit tests use mocked LLMs and mocked HTTP transport (no real API calls).
- [ ] `poetry run pytest` passes with 100% success.
- [ ] Tests verify `wants_to_schedule` propagation

## Follow-up — 2026-06-30T11:44:36Z

Replace the static 1-second debounce in the CareFlow AI webhook processor with a **resetable Redis-based debounce** of configurable duration (default 30s), ensuring that only a period of true user silence triggers LangGraph execution. Buffered messages must be consolidated using newline separators before being passed to the supervisor.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

## Context

The codebase already has:
- `POST /api/v1/webhook/whatsapp` in `app/api/webhook.py` that buffers messages into a tenant `message_buffer` table and dispatches a `BackgroundTasks` job.
- A `process_message_debounce` function implementing a static `asyncio.sleep(1)`.
- `RedisSessionManager` (`session_manager`) with a redis client accessible via `session_manager.get_client()`.
- 96 existing passing tests.

## Requirements

### R1. Resetable Debounce via Redis Timestamp
- Add a configurable environment variable `DEBOUNCE_SECONDS` (default `30`) to the app settings.
- On each incoming webhook message, after writing to `message_buffer`, write the current timestamp (Unix float epoch) to a Redis key: `last_msg_time:{organization_id}:{phone_number}`.
- The background task waits `DEBOUNCE_SECONDS` seconds, then re-reads the key.
- If `current_time - last_msg_time >= DEBOUNCE_SECONDS`, the silence window has been reached: proceed to acquire the Redis mutex lock and process the buffer.
- If `current_time - last_msg_time < DEBOUNCE_SECONDS`, a newer message arrived during the wait: exit silently. The background task started by that newer message will handle processing.

### R2. Newline-Joined Message Consolidation
- When consuming all messages from `message_buffer`, join them with `\n` (newline) instead of space (` `).

## Acceptance Criteria

### Tests & Verification
- [ ] File `tests/test_debounce_resetable.py` is created.
- [ ] Tests set `DEBOUNCE_SECONDS = 2` via monkeypatch or env override for fast execution.
- [ ] A test scenario simulates 3 messages at `t=0`, `t=0.5s`, `t=1s`:
  - LangGraph is invoked **exactly once**.
  - Invocation happens approximately at `t=3s` (i.e., `DEBOUNCE_SECONDS` after the last message).
  - All 3 messages appear newline-joined in the prompt input.
- [ ] `poetry run pytest` passes with 100% success (≥ 97 total tests).

## Follow-up — 2026-06-30T14:42:11Z

Implement human intervention detection, CRM status synchronization, and duplicate card cleanup in the CareFlow AI WhatsApp webhook pipeline.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

## Context

The codebase already has:
- `POST /api/v1/webhook/whatsapp` in `app/api/webhook.py` with a resetable Redis debounce and mutex lock (100 tests passing).
- `app/services/whatsapp_client.py`: stub for sending WhatsApp messages.
- `app/services/medflow_client.py`: `MedflowClient` with `book_appointment` and `patch_appointment_status`.
- `app/models/whatsapp.py`: `ClientData` (table `dados_cliente`) with `phone_number` and `status` columns.
- `RedisSessionManager` (`session_manager`) with `get_client()` for direct Redis access.
- API contracts documented in `CareFlow AI/docs/medflow_api_contracts.md`.

## R3 Resolution — Card Cancellation

> **Resolved:** Use **Option A** — call `POST /api/webhooks/n8n/cancel-appointment/{id}` to set the original `EM_CONTATO` card to `CANCELADO`, which removes it from the active CRM view. This uses the documented, tested endpoint.

---

## Requirements

### R1. Human Intervention Detection (Bot Self-Reply Protection)
The webhook must handle outgoing messages (`fromMe = True`) sent through the clinic's own WhatsApp number:
- When `WhatsAppClient` sends a bot reply, it must persist a short-lived marker in Redis: `bot_sending:{organization_id}:{phone_number}` with a TTL of 5 seconds.
- When the webhook receives an outgoing event (`fromMe = True`), check for the `bot_sending` key:
  - If the key **exists**: it is a bot self-reply — ignore the event entirely.
  - If the key **does not exist**: it is a human agent reply — set `bot_active = False` in the Redis conversation session and update `dados_cliente.status` to `ATENDIMENTO_HUMANO` in the tenant database.

### R2. LangGraph Human Escalation Sync
When the LangGraph supervisor returns a decision to escalate to human support (e.g., `next_phase = "human"` or `suggested_action = "escalar_humano"`):
- Set `bot_active = False` in the Redis session.
- Update `dados_cliente.status` to `ATENDIMENTO_HUMANO` in the tenant database.
- Call `MedflowClient.patch_appointment_status(appointment_id, "ATENDIMENTO_HUMANO")` to move the CRM card to the human support column.

### R3. Cleanup of Duplicate EM_CONTATO Card After Booking
After `agenda_node` successfully books an appointment via `MedflowClient.book_appointment` (creating a new `AGENDADO` card):
- Retrieve the original appointment ID of the `EM_CONTATO` card (stored in the Redis session or `dados_cliente` at first-contact time).
- Cancel the original card by calling `MedflowClient.cancel_appointment(original_appointment_id)` which maps to `POST /api/webhooks/n8n/cancel-appointment/{appointmentId}`.

## Acceptance Criteria

### Tests & Verification
- [ ] File `tests/test_human_intervention.py` is created.
- [ ] Test 1 — **Bot self-reply ignored:** Webhook receives `fromMe = True` **with** `bot_sending` key active in Redis → `bot_active` remains `True`, `dados_cliente.status` is **not** changed.
- [ ] Test 2 — **Human takeover detected:** Webhook receives `fromMe = True` **without** `bot_sending` key → `bot_active` is set to `False`, `dados_cliente.status` updated to `ATENDIMENTO_HUMANO`.
- [ ] Test 3 — **Card cleanup after booking:** After `agenda_node` books an appointment, `MedflowClient.cancel_appointment` is called with the original `EM_CONTATO` card ID.
- [ ] `poetry run pytest` passes with 100% success (≥ 103 total tests).

## Follow-up — 2026-06-30T15:20:00Z

CareFlow AI, um microssistema de agentes em Python/FastAPI integrado ao Medflow. Este subprojeto executa a Fase 5 (Prompt 5.1) focado em Cobertura de Código e Simulação de Carga concorrente com validação de Debounce e Locks.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

## Requirements

### R1. Configuração de Cobertura de Código (Code Coverage)
Adicionar `pytest-cov` ao Poetry como dependência de desenvolvimento e configurar o `pyproject.toml` ou as opções do pytest para que a execução dos testes gere automaticamente relatórios de cobertura do diretório `app/`. O relatório deve ser gerado no terminal e como arquivos XML/HTML em `htmlcov/`.

### R2. Script de Simulação de Carga (scripts/simulate_load.py)
Desenvolver um script independente em Python (`scripts/simulate_load.py`) usando `asyncio` e `httpx` para simular 10 números de WhatsApp concorrentes enviando fluxos de mensagens rápidas e picadas para um mesmo tenant.
O script deve:
1. Disparar requisições para o endpoint `/api/v1/webhook/whatsapp`.
2. Simular mensagens rápidas enviadas pelo mesmo paciente com intervalo de 0.5s para validar o debounce de 30 segundos (o script deve esperar o debounce real para validar se o agrupamento foi executado e as mensagens processadas como um único bloco agregador).
3. Utilizar configurações/credenciais/cabeçalhos de um tenant de teste e direcionar a simulação para o servidor FastAPI rodando localmente (por padrão `http://localhost:8000`).
4. Exibir um relatório final no terminal detalhando:
   - Total de webhooks enviados.
   - Tempo médio de resposta do webhook (que deve ser < 500ms).
   - Status de sucesso/falha de processamento no banco do tenant (verificar se as mensagens foram processadas corretamente no banco após o debounce).

## Acceptance Criteria

### Code Coverage
- [ ] O relatório de cobertura do `pytest-cov` está configurado no projeto.
- [ ] A cobertura total de linhas de código sob o diretório `app/` é superior a 90% (excluindo arquivos irrelevantes/gerados, caso existam, que tenham sido configurados no `pyproject.toml`).

### Load Simulation Script
- [ ] O script `scripts/simulate_load.py` roda localmente sem erros e gera o relatório consolidado com sucesso.
- [ ] O script simula corretamente a concorrência de 10 números enviando mensagens picadas com intervalo de 0.5s.
- [ ] O script valida que as mensagens são agrupadas em um único processamento pelo debounce de 30 segundos, verificando no banco de dados o status do processamento após a expiração do debounce.
- [ ] O tempo médio de resposta de cada webhook retornado pelo script é menor que 500ms.

### Test Execution
- [ ] O comando `poetry run pytest` passa com 100% de sucesso (incluindo todos os 103 testes originais mais eventuais novos testes adicionados).



## Follow-up — 2026-07-01T16:58:47Z

Configuração de monitoramento e tracing de LLM para o CareFlow AI, incluindo instrumentação de métricas com Prometheus, logs estruturados via módulo padrão python `logging` para a execução do grafo LangGraph, e integração do LangSmith.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

## Requirements

### R1. Integração de Métricas com Prometheus
- Adicione a dependência `prometheus-fastapi-instrumentator` ao Poetry e instale-a.
- Inicialize a instrumentação no arquivo `app/main.py` para coletar métricas de requisições HTTP da API.
- Exponha o endpoint `/metrics` no FastAPI para coletas do Prometheus.

### R2. Rastreamento e Logs do LangGraph (Stdout Tracing)
- Configure logs estruturados usando o módulo padrão de `logging` do Python para a execução do grafo.
- Toda vez que o grafo do LangGraph for invocado, os logs devem registrar no nível INFO ou adequado:
  * O timestamp do log.
  * O ID da sessão (`phone_number`).
  * A ordem sequencial dos nós percorridos (ex: `[LangGraph Trace] Session 55... | Node: supervisor_node -> Node: crc_sdr_node -> Node: supervisor_node -> END`).
  * O tempo total de processamento de cada nó em milissegundos.

### R3. Integração com LangSmith (Cloud Tracing)
- Configure a leitura das seguintes variáveis de ambiente no arquivo `app/core/config.py` e `.env` para carregar as configurações de tracing no `Settings` do Pydantic, permitindo que o ecossistema LangChain/LangSmith as leia e rastreie automaticamente:
  * `LANGCHAIN_TRACING_V2=true`
  * `LANGCHAIN_API_KEY=<chave>`
  * `LANGCHAIN_PROJECT=<nome_do_projeto>`

## Acceptance Criteria

### Monitoring and Verification
- [ ] O arquivo `tests/test_monitoring.py` contendo testes unitários e de integração deve ser criado.
- [ ] O teste deve validar que `GET /metrics` retorna HTTP 200 e texto contendo métricas do Prometheus (ex: `http_requests_total`).
- [ ] O teste deve validar que ao invocar o grafo LangGraph, logs contendo o formato esperado de percurso de nós e tempos em milissegundos são emitidos no log do sistema (capturável via fixture `caplog` do pytest).
- [ ] Executar `poetry run pytest` deve passar com 100% de sucesso (somando os novos testes de monitoramento aos anteriores).



