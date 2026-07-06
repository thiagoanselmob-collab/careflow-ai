# Original User Request

## 2026-07-05T10:01:18Z

Refatorar o grafo LangGraph do CareFlow AI para ler prompts e modelos de LLM dinamicamente do banco de dados do tenant, remover o `rag_node` como nó direto e converter todos os nós para async.

Working directory: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend`
Integrity mode: development

## Requirements

### R1. Remover `rag_node` do StateGraph e tornar `buscar_conhecimento` uma tool compartilhada
O grafo deve passar a ter apenas 3 nós de diálogo: `supervisor`, `crc_sdr_node`, `agenda_node`. O `rag_node` deve ser removido do StateGraph (remover `workflow.add_node("rag_node", ...)`, a aresta condicional e a aresta de retorno). A função `buscar_conhecimento(query, organization_id)` já existe no arquivo e deve ser chamada internamente pelo `crc_sdr_node` e pelo `agenda_node` quando o paciente trouxer dúvidas institucionais (preços, convênios, regras). O `RoutingDecision` do supervisor deve ser atualizado para não incluir mais `rag_node` como opção de roteamento. Manter a implementação de `buscar_conhecimento` e `_async_rag_node` no arquivo para backward-compatibility, mas remover `rag_node` do grafo compilado.

### R2. Converter todos os nós para async
Os nós `supervisor_node` e `crc_sdr_node` atualmente são síncronos (`def`). Devem ser convertidos para o mesmo padrão async já usado pelo `agenda_node`: uma implementação interna `async def _async_supervisor_node(...)` / `async def _async_crc_sdr_node(...)` + um wrapper síncrono com `@log_node_execution` que despacha via `ThreadPoolExecutor + asyncio.run`. Isso unifica o padrão e permite chamar `get_agent_config()` e `buscar_conhecimento()` de forma nativa.

### R3. Leitura dinâmica da configuração do agente do banco do tenant
Criar uma função auxiliar assíncrona `get_agent_config(tenant_id: str, agent_type: str) -> Optional[AgentConfig]` que:
- Usa o `tenant_db_manager` existente em `app.core.tenant_database` para abrir sessão do tenant
- Busca o registro da tabela `agent_configs` correspondente ao `agent_type`
- Retorna `None` se não encontrar (para que o fallback funcione)

Essa função deve ser usada dentro de `_async_supervisor_node`, `_async_crc_sdr_node` e `_async_agenda_node` para carregar `system_prompt`, `llm_provider` e `llm_model` dinamicamente.

### R4. Inicialização dinâmica da LLM por provider e modelo
Com base nos campos `llm_provider` e `llm_model` do `AgentConfig`:
- `google` → `ChatGoogleGenerativeAI(model=llm_model)` (já está nas dependências)
- `openai` → `ChatOpenAI(model=llm_model)` (adicionar `langchain-openai` ao `pyproject.toml` se necessário)
- `anthropic` → `ChatAnthropic(model=llm_model)` (já está nas dependências)

Se o banco não tiver configuração cadastrada para aquele agente, usar fallback: `google` como provider e `gemini-1.5-flash` como model.

Criar uma função factory `get_llm_from_config(agent_config: Optional[AgentConfig])` que encapsula essa lógica e retorna a instância da LLM correta.

### R5. Remover prompts hardcoded e usar `system_prompt` do banco
- Remover o `DEFAULT_SDR_PROFILE` (dicionário hardcoded com doctor_name, clinic_name, etc.)
- No `_async_crc_sdr_node`: usar o `system_prompt` carregado do banco como prompt do agente. Se não houver `system_prompt` no banco, usar um fallback razoável inline.
- No `_async_supervisor_node`: usar o `system_prompt` do banco para o prompt de roteamento. Fallback para o prompt atual se não existir no banco.
- No `_async_agenda_node`: usar o `system_prompt` do banco. Fallback para o prompt atual se não existir.

### R6. Manter todos os testes existentes passando e escrever novos testes
- Nenhuma mudança deve quebrar os 185 testes já existentes
- Escrever novos testes unitários validando:
  - `get_agent_config()` retorna config correta do banco
  - `get_agent_config()` retorna `None` quando não há registro
  - `get_llm_from_config()` retorna `ChatGoogleGenerativeAI` para provider `google`
  - `get_llm_from_config()` retorna `ChatOpenAI` para provider `openai`
  - `get_llm_from_config()` retorna `ChatAnthropic` para provider `anthropic`
  - `get_llm_from_config(None)` retorna o fallback (`ChatGoogleGenerativeAI`)
  - O grafo compilado NÃO contém `rag_node` como nó
  - O `RoutingDecision` não aceita `rag_node` como valor de `next_node`
- Comando de validação: `poetry run pytest`

## Contexto Técnico

- **Arquivo alvo**: `app/services/agents/graph.py` (1143 linhas)
- **Modelo de configuração**: `app/models/agent_config.py` → `AgentConfig` com campos `agent_type`, `system_prompt`, `llm_provider`, `llm_model`, `is_active`
- **Tenant DB Manager**: `app.core.tenant_database.tenant_db_manager` (já existe, usado em outros pontos do graph.py)
- **Dependências existentes no pyproject.toml**: `langchain-google-genai`, `langchain-anthropic` — pode ser necessário adicionar `langchain-openai`
- **Suite de testes**: 185 testes em `tests/` — todos devem continuar passando
- **Testes do grafo existentes**: `tests/test_agent_graph.py`, `tests/test_agent_agenda.py`, `tests/test_agent_rag.py`
- **Stack**: Python + FastAPI + SQLAlchemy async + LangGraph + LangChain + Poetry

## Acceptance Criteria

### Grafo atualizado sem `rag_node`
- [ ] `rag_node` removido do StateGraph (`workflow.add_node`, `workflow.add_conditional_edges`, `workflow.add_edge`)
- [ ] `RoutingDecision.next_node` atualizado para `Literal["crc_sdr_node", "agenda_node", "END"]`
- [ ] `buscar_conhecimento()` chamado internamente por `crc_sdr_node` e `agenda_node`
- [ ] `check_next_node()` não referencia mais `rag_node`

### Nós convertidos para async
- [ ] `_async_supervisor_node` + wrapper síncrono `supervisor_node`
- [ ] `_async_crc_sdr_node` + wrapper síncrono `crc_sdr_node`
- [ ] Padrão unificado com `agenda_node` que já usa esse pattern

### Leitura dinâmica de configs
- [ ] `get_agent_config()` implementada e functional
- [ ] Cada nó carrega `system_prompt`, `llm_provider`, `llm_model` do banco
- [ ] Fallback funciona quando não há config no banco (google/gemini-1.5-flash)

### LLM factory dinâmica
- [ ] `get_llm_from_config()` implementada
- [ ] Suporta `google`, `openai`, `anthropic`
- [ ] `langchain-openai` adicionado ao pyproject.toml se necessário

### Prompts hardcoded removidos
- [ ] `DEFAULT_SDR_PROFILE` removido
- [ ] Prompts construídos a partir do `system_prompt` do banco

### Regressão zero + novos testes
- [ ] `poetry run pytest` executa sem falhas (0 failures, 0 errors)
- [ ] Novos testes para `get_agent_config`, `get_llm_from_config`, ausência de `rag_node` no grafo
- [ ] Nenhum teste existente removido ou desabilitado

- [ ] `get_llm_from_config()` implementada
- [ ] Suporta `google`, `openai`, `anthropic`
- [ ] `langchain-openai` adicionado ao pyproject.toml se necessário

### Prompts hardcoded removidos
- [ ] O teste deve validar que ao invocar o grafo LangGraph, logs contendo o formato esperado de percurso de nós e tempos em milissegundos são emitidos no log do sistema (capturável via fixture `caplog` do pytest).
- [ ] Executar `poetry run pytest` deve passar com 100% de sucesso (somando os novos testes de monitoramento aos anteriores).

## 2026-07-05T17:30:29Z

Implementar os endpoints administrativos REST no backend FastAPI para leitura e atualização das configurações dos 5 agentes de IA por tenant, conectando o painel admin ao banco de dados de cada clínica.

Working directory: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend`
Integrity mode: development

## Requirements

### R1. Schemas Pydantic para validação (`app/schemas/agent_config.py`)
Criar dois schemas:
- `AgentConfigResponse`: retorna todos os campos do `AgentConfig` (id, agent_type, system_prompt, system_prompt_noshow, llm_provider, llm_model, is_active, reminder_time, reminder_rules, updated_at)
- `AgentConfigUpdate`: campos todos opcionais para PATCH/PUT parcial — `system_prompt`, `system_prompt_noshow`, `llm_provider` (validar: aceita apenas `'openai'`, `'google'`, `'anthropic'`), `llm_model`, `is_active`, `reminder_time` (validar formato `HH:MM`, ex: `"11:00"`), `reminder_rules` (validar que é uma string JSON representando lista de inteiros, ex: `"[1, 5]"`)

### R2. Rotas admin de configuração de agentes
Criar as rotas seguindo o mesmo padrão de autenticação já existente no projeto: tenant identificado via header `X-Tenant-ID` ou query param `organization_id` (igual ao `/api/v1/admin/knowledge`):

- **`GET /api/v1/admin/agents`**: retorna lista com as configurações dos 5 tipos de agentes (`supervisor`, `sdr`, `agenda`, `reminders`, `followup`) para o tenant. Se não houver registro no banco do tenant para um tipo, incluí-lo na resposta com valores default (`llm_provider='google'`, `llm_model='gemini-1.5-flash'`, `is_active=True`, demais campos `null`).

- **`PUT /api/v1/admin/agents/{agent_type}`**: atualiza os campos enviados no body para o agente especificado no banco do tenant. Se não houver registro, criá-lo (upsert). Retorna o `AgentConfigResponse` atualizado. Retornar HTTP 400 para `agent_type` inválido ou falha de validação.

### R3. Registro das rotas em `app/main.py`
As novas rotas devem ser registradas no `app/main.py` com `app.include_router(...)`.

### R4. Testes em `tests/test_agent_configs_api.py`
Escrever testes de integração HTTP cobrindo:
- `GET /api/v1/admin/agents` com tenant que tem configs salvas → retorna todos os 5 agentes com dados corretos
- `GET /api/v1/admin/agents` com tenant sem configs → retorna todos os 5 agentes com valores default
- `PUT /api/v1/admin/agents/reminders` com `reminder_time="11:00"` e `reminder_rules="[1, 5]"` → 200 OK, dados salvos
- `PUT /api/v1/admin/agents/reminders` com `reminder_time="25:99"` → 422 Unprocessable Entity
- `PUT /api/v1/admin/agents/reminders` com `reminder_rules="not-json"` → 422 Unprocessable Entity
- `PUT /api/v1/admin/agents/invalid_type` → 400 Bad Request
- Isolamento multi-tenant: atualizar prompt do org_1 não afeta org_2
- Comando: `poetry run pytest` → 0 failures, 0 errors

## Contexto Técnico

- **Padrão auth existente**: `X-Tenant-ID` header ou `organization_id` query param (ver `app/api/knowledge.py` → `get_tenant_id()`)
- **AgentConfig model**: `app/models/agent_config.py` — campos mapeados, validação de `agent_type` via `@validates`
- **Tenant DB Manager**: `app/core/tenant_database.py` → `tenant_db_manager.get_tenant_session(org_id)`
- **Schemas existentes**: `app/schemas/session.py` — referência de estilo Pydantic v2
- **Tipos válidos de agente**: `'supervisor'`, `'sdr'`, `'agenda'`, `'reminders'`, `'followup'`
- **Suite atual**: 215 testes passando — todos devem continuar passando
- **Stack**: Python + FastAPI + SQLAlchemy async + Pydantic v2 + Poetry

## Acceptance Criteria

### Schemas válidos
- [ ] `AgentConfigResponse` cobre todos os 10 campos do modelo
- [ ] `AgentConfigUpdate` valida `llm_provider` (enum: openai/google/anthropic)
- [ ] `AgentConfigUpdate` valida `reminder_time` como `HH:MM`
- [ ] `AgentConfigUpdate` valida `reminder_rules` como JSON string de lista de inteiros

### Endpoints funcionais
- [ ] `GET /api/v1/admin/agents` retorna lista de 5 itens sempre
- [ ] Agentes sem config retornam valores default (não erro)
- [ ] `PUT /api/v1/admin/agents/{agent_type}` faz upsert no banco do tenant
- [ ] `PUT` com `agent_type` inválido retorna HTTP 400
- [ ] `PUT` com campos inválidos retorna HTTP 422

### Rotas registradas
- [ ] Router importado e registrado em `app/main.py`

### Isolamento multi-tenant
- [ ] Atualização de org_1 não afeta dados de org_2

### Regressão zero + novos testes
- [ ] `poetry run pytest` → 0 failures, 0 errors
- [ ] Todos os cenários do R4 cobertos em `tests/test_agent_configs_api.py`g`, `get_llm_from_config`, ausência de `rag_node` no grafo
- [ ] Nenhum teste existente removido ou desabilitado

## 2026-07-05T19:42:39Z

Implementar os endpoints administrativos REST no backend FastAPI para leitura e atualização das configurações dos 5 agentes de IA por tenant, conectando o painel admin ao banco de dados de cada clínica.

Working directory: `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend`
Integrity mode: development

## Decisões já tomadas (Socratic Gate resolvido)

1. **Validação de `reminder_time`**: Validar limites lógicos de tempo (horas entre 00 e 23, minutos entre 00 e 59) além de correspondência ao padrão HH:MM.
2. **Validação de `reminder_rules`**: Garantir que seja um JSON válido que resolve para uma lista de inteiros positivos maiores que zero.
3. **Padrão de Autenticação**: Usar e importar a função `get_tenant_id()` existente em `app/api/knowledge.py` para consistência de extração/validação do Tenant ID (via header `X-Tenant-ID` ou query param `organization_id`).

## Requirements

### R1. Schemas Pydantic para validação (`app/schemas/agent_config.py`)
Criar dois schemas:
- `AgentConfigResponse`: retorna todos os campos do `AgentConfig` (id, agent_type, system_prompt, system_prompt_noshow, llm_provider, llm_model, is_active, reminder_time, reminder_rules, updated_at)
- `AgentConfigUpdate`: campos todos opcionais para PUT parcial — `system_prompt`, `system_prompt_noshow`, `llm_provider` (enum: apenas `'openai'`, `'google'`, `'anthropic'`), `llm_model`, `is_active`, `reminder_time` (validar formato `HH:MM` com horas 00-23 e minutos 00-59), `reminder_rules` (validar que é uma string JSON representando lista de inteiros positivos maiores que zero)

### R2. Rotas admin de configuração de agentes
Criar as rotas reutilizando `get_tenant_id()` de `app/api/knowledge.py` para identificação do tenant via header `X-Tenant-ID` or query param `organization_id`:

- **`GET /api/v1/admin/agents`**: retorna lista com as configurações dos 5 tipos de agentes (`supervisor`, `sdr`, `agenda`, `reminders`, `followup`) para o tenant. Se não houver registro no banco do tenant para um tipo, incluí-lo na resposta com valores default (`llm_provider='google'`, `llm_model='gemini-1.5-flash'`, `is_active=True`, demais campos `null`).

- **`PUT /api/v1/admin/agents/{agent_type}`**: atualiza os campos enviados no body para o agente especificado no banco do tenant. Se não houver registro, criá-lo (upsert). Retorna o `AgentConfigResponse` atualizado. Retornar HTTP 400 para `agent_type` inválido. Retornar HTTP 422 para falha de validação de campos.

### R3. Registro das rotas em `app/main.py`
As novas rotas devem ser registradas no `app/main.py` com `app.include_router(...)`.

### R4. Testes em `tests/test_agent_configs_api.py`
Escrever testes de integração HTTP cobrindo:
- `GET /api/v1/admin/agents` com tenant que tem configs salvas → retorna todos os 5 agentes com dados corretos
- `GET /api/v1/admin/agents` com tenant sem configs → retorna todos os 5 agentes com valores default
- `PUT /api/v1/admin/agents/reminders` com `reminder_time="11:00"` e `reminder_rules="[1, 5]"` → 200 OK, dados salvos
- `PUT /api/v1/admin/agents/reminders` com `reminder_time="25:99"` → 422
- `PUT /api/v1/admin/agents/reminders` com `reminder_time="23:60"` → 422 (minutos inválidos)
- `PUT /api/v1/admin/agents/reminders` com `reminder_rules="not-json"` → 422
- `PUT /api/v1/admin/agents/reminders` com `reminder_rules="[-1, 5]"` → 422 (não positivo)
- `PUT /api/v1/admin/agents/invalid_type` → 400
- Isolamento multi-tenant: atualizar prompt do org_1 não afeta org_2
- `poetry run pytest` → 0 failures, 0 errors

## Contexto Técnico

- **Padrão auth existente**: `get_tenant_id()` em `app/api/knowledge.py`
- **AgentConfig model**: `app/models/agent_config.py`
- **Tenant DB Manager**: `app/core/tenant_database.py` → `tenant_db_manager.get_tenant_session(org_id)`
- **Schemas existentes**: `app/schemas/session.py` — referência de estilo Pydantic v2
- **Tipos válidos de agente**: `'supervisor'`, `'sdr'`, `'agenda'`, `'reminders'`, `'followup'`
- **Suite atual**: 215 testes passando — todos devem continuar passando
- **Stack**: Python + FastAPI + SQLAlchemy async + Pydantic v2 + Poetry

## Acceptance Criteria

### Schemas válidos
- [ ] `AgentConfigResponse` cobre todos os 10 campos do modelo
- [ ] `AgentConfigUpdate` valida `llm_provider` (enum: openai/google/anthropic)
- [ ] `AgentConfigUpdate` valida `reminder_time` com formato HH:MM e limites lógicos (00-23h, 00-59min)
- [ ] `AgentConfigUpdate` valida `reminder_rules` como JSON string de lista de inteiros positivos

### Endpoints funcionais
- [ ] `GET /api/v1/admin/agents` retorna lista de exatamente 5 itens sempre
- [ ] Agentes sem config retornam valores default (não erro)
- [ ] `PUT /api/v1/admin/agents/{agent_type}` faz upsert no banco do tenant
- [ ] `PUT` com `agent_type` inválido retorna HTTP 400
- [ ] `PUT` com campos inválidos retorna HTTP 422

### Rotas registradas
- [ ] Router importado e registrado em `app/main.py`

### Isolamento multi-tenant
- [ ] Atualização de org_1 não afeta dados de org_2

### Regressão zero + novos testes
- [ ] `poetry run pytest` → 0 failures, 0 errors
- [ ] Todos os cenários do R4 cobertos em `tests/test_agent_configs_api.py`
- [ ] Nenhum teste existente removido ou desabilitado
