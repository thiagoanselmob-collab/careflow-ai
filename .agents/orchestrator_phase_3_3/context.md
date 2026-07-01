# Context - CareFlow AI Phase 3.3

## Existing Files & Code Structure
- **Settings**: Defined in `app/core/config.py`. Needs extension for Medflow Central API configurations (`medflow_api_url`, `medflow_jwt_token`).
- **LangGraph Agent Workflow**: Located in `app/services/agents/graph.py`.
  - State: `AgentState` contains `messages`, `bot_active`, `collected_data`, `wants_to_schedule`, `next_node`, `action_required`.
  - Supervisor: `supervisor_node` routes inputs.
  - SDR Node: `crc_sdr_node` performs multi-tenant persona communication using ChatAnthropic.
  - Agenda Node: `agenda_node` is currently a stub that needs to be replaced.
- **Client Integration**: A new HTTP client `MedflowClient` must be created in `app/services/medflow_client.py` using `httpx.AsyncClient`.
- **Test Files**:
  - `tests/test_agent_graph.py` contains supervisor tests.
  - `tests/test_sdr_node.py` contains SDR node tests.
  - A new test file `tests/test_agent_agenda.py` must be added.

## Dependencies & Environments
- LangGraph, LangChain, and standard dependencies are loaded.
- `httpx` is used for client requests.
- `ChatGoogleGenerativeAI` is used for Gemini LLM calls.
- Timezone reference: `America/Sao_Paulo` (crucial for relative date calculations).
- Security: Token authorization header and idempotency header must be handled.
