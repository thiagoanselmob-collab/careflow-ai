# Original User Request

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
Criar as rotas reutilizando `get_tenant_id()` de `app/api/knowledge.py` para identificação do tenant via header `X-Tenant-ID` ou query param `organization_id`:

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
