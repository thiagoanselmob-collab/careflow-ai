# Original User Request

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
- [ ] Tests verify `wants_to_schedule` propagation

## Follow-up — 2026-06-30T01:56:00Z

Implement the WhatsApp webhook receiver for CareFlow AI in FastAPI. This includes dynamic tenant database message buffering for double-texting debounce, Redis distributed locking to prevent concurrency race conditions, and executing the patient conversation history via the LangGraph supervisor.

Working directory: /Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend
Integrity mode: development

## Requirements

### R1. Webhook Endpoint
- Create a `POST /api/v1/webhook/whatsapp` route in FastAPI.
- Extract `organization_id` (via query param or headers) and the sender's `phone_number`.
- Return HTTP 200 OK immediately (<500ms) after buffering the message without waiting for the LLM graph execution.

### R2. PostgreSQL Dynamic Message Buffer (`message_buffer` table) and Client Table (`dados_cliente`)
- Create and map the following tenant-specific models in `app/models/`:
  - `MessageBuffer` (table `message_buffer`): Store buffered messages. Must include `id`, `phone_number`, `content`, `created_at`.
  - `ClientData` (table `dados_cliente`): Store client registration information. Must include `phone_number`, `status` (or `atendimento_ia` as applicable), `created_at`.
- Dynamically create these tables in the tenant's database schema if they don't exist during tenant pool initialization (update `_init_tenant_db` in `app/core/tenant_database.py`).
- Each webhook request inserts the incoming message content into the tenant's `message_buffer` table.
- A FastAPI `BackgroundTasks` job is triggered to process the buffer after a 1-second debounce.

### R3. Redis Mutex Lock
- After the 1-second debounce, attempt to acquire a short-lived exclusive Redis lock.
- To avoid collision with the Redis session key `{organization_id}:{phone_number}`, use the lock key format: `{organization_id}:{phone_number}:lock`.
- The lock must prevent concurrent workers from processing the same patient's buffer.
- With the lock, read all buffered messages for the patient, consolidate/concatenate them, delete the read messages from the `message_buffer` table, and release the lock.

### R4. Graph Execution and Messaging
- Check if the patient exists in the tenant's `dados_cliente` table.
- If not, write a new client record to `dados_cliente` and invoke CRM registration via `MedflowClient.book_appointment` (using placeholder/default parameters like a default doctor, current date/time) to initialize the CRM status to `EM_CONTATO`.
- Load the conversation session from Redis (using the existing `RedisSessionManager`), execute LangGraph with the consolidated input, save the updated session back to Redis, and send the response back via the `WhatsAppClient` service.
- Implement a service stub `app/services/whatsapp_client.py simulating message sending.

## Acceptance Criteria

### Execution & Verification
- [ ] Created `tests/test_webhook_queue.py` validating:
  - The webhook endpoint responds HTTP 200 in less than 500ms.
  - Multiple sequential messages from the same sender are successfully debounced and aggregated into a single graph execution.
  - The Redis lock (using `fakeredis` in tests) prevents concurrent execution runs for the same sender.
- [ ] The model files and dynamic tenant db setup successfully support both SQLite (in tests) and PostgreSQL (in production).
- [ ] Running `poetry run pytest` passes with 100% success, bringing the total number of passing tests above 88.

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


