#!/usr/bin/env python3
import asyncio
import time
import sys
import os
import argparse
from typing import List, Dict, Any
import httpx
from sqlalchemy import select, text, bindparam
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Ensure the root directory is in the path to allow imports of CareFlow backend modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings
from app.services.encryption import decrypt_data
from app.models.settings import Settings


async def send_webhook(
    client: httpx.AsyncClient,
    base_url: str,
    tenant_id: str,
    phone: str,
    content: str
) -> float:
    """
    Sends a single webhook request and returns the latency in seconds.
    """
    url = f"{base_url.rstrip('/')}/api/v1/webhook/whatsapp"
    params = {"organization_id": tenant_id}
    payload = {
        "phone_number": phone,
        "content": content
    }
    
    start_time = time.perf_counter()
    try:
        response = await client.post(url, params=params, json=payload, timeout=10.0)
        latency = time.perf_counter() - start_time
        if response.status_code != 200:
            print(f"[ERROR] Webhook failed for {phone} with status {response.status_code}: {response.text}")
            return -1.0
        return latency
    except Exception as e:
        latency = time.perf_counter() - start_time
        print(f"[ERROR] Connection failed for {phone}: {e}")
        return -1.0


async def simulate_phone_load(
    client: httpx.AsyncClient,
    base_url: str,
    tenant_id: str,
    phone: str,
    num_messages: int
) -> List[float]:
    """
    Simulates rapid/fragmented messages sent by a single phone number with an interval of 0.5s.
    """
    latencies = []
    for i in range(1, num_messages + 1):
        content = f"Fragment {i} from number {phone}"
        latency = await send_webhook(client, base_url, tenant_id, phone, content)
        if latency >= 0:
            latencies.append(latency)
        if i < num_messages:
            await asyncio.sleep(0.5)
    return latencies


async def run_load(
    base_url: str,
    tenant_id: str,
    num_phones: int,
    num_messages: int
) -> tuple[List[float], List[str]]:
    """
    Simulates load across multiple phone numbers concurrently.
    Returns the list of latencies and the list of phone numbers simulated.
    """
    phones = [f"+55119900000{i:02d}" for i in range(1, num_phones + 1)]
    async with httpx.AsyncClient() as client:
        tasks = []
        for phone in phones:
            tasks.append(simulate_phone_load(client, base_url, tenant_id, phone, num_messages))
        
        results = await asyncio.gather(*tasks)
        # Flatten the list of latencies
        flat_latencies = [lat for phone_lats in results for lat in phone_lats]
        return flat_latencies, phones


async def verify_database(tenant_id: str, simulated_phones: List[str]) -> Dict[str, Any]:
    """
    Verifies that the tenant database has processed all buffered messages
    and stored/updated the appropriate client records.
    Specifically checks existence of the simulated phone numbers.
    """
    # Create engine to read tenant connection string from the central database
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    
    try:
        async with session_maker() as session:
            stmt = select(Settings).where(Settings.organization_id == tenant_id)
            res = await session.execute(stmt)
            setting = res.scalar_one_or_none()
            if not setting:
                raise ValueError(f"Tenant configuration not found for organization: {tenant_id}")
            encrypted_conn_str = setting.tenant_connection_string
            
        decrypted_conn_str = decrypt_data(encrypted_conn_str)
        if decrypted_conn_str.startswith("postgresql://"):
            decrypted_conn_str = decrypted_conn_str.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif decrypted_conn_str.startswith("postgres://"):
            decrypted_conn_str = decrypted_conn_str.replace("postgres://", "postgresql+asyncpg://", 1)
        elif decrypted_conn_str.startswith("sqlite://"):
            decrypted_conn_str = decrypted_conn_str.replace("sqlite://", "sqlite+aiosqlite://", 1)
            
        tenant_engine = create_async_engine(decrypted_conn_str, echo=False, future=True)
        
        async with tenant_engine.connect() as conn:
            # 1. Check remaining messages in buffer for these phone numbers (should be 0)
            buf_stmt = text("SELECT COUNT(*) FROM message_buffer WHERE phone_number IN :phones").bindparams(
                bindparam("phones", expanding=True)
            )
            buf_res = await conn.execute(buf_stmt, {"phones": tuple(simulated_phones)})
            buffer_count = buf_res.scalar() or 0
            
            # 2. Check total messages in buffer overall (should be 0 for a clean test)
            total_buf_res = await conn.execute(text("SELECT COUNT(*) FROM message_buffer"))
            total_buffer_count = total_buf_res.scalar() or 0
            
            # 3. Check status of specifically simulated phone numbers
            clients_stmt = text("SELECT phone_number, status FROM dados_cliente WHERE phone_number IN :phones").bindparams(
                bindparam("phones", expanding=True)
            )
            clients_res = await conn.execute(clients_stmt, {"phones": tuple(simulated_phones)})
            clients = clients_res.fetchall()
            found_phones = {row[0]: row[1] for row in clients}
            
        await tenant_engine.dispose()
        
        # Verify all simulated phones are registered
        all_clients_registered = all(phone in found_phones for phone in simulated_phones)
        success = (buffer_count == 0 and all_clients_registered)
        
        return {
            "success": success,
            "buffer_count": buffer_count,
            "total_buffer_count": total_buffer_count,
            "registered_count": len(found_phones),
            "missing_phones": [p for p in simulated_phones if p not in found_phones],
            "found_phones": found_phones
        }
    finally:
        await engine.dispose()


def calculate_percentile(sorted_list: List[float], percentile: float) -> float:
    """
    Calculates the percentile value of a sorted list of floats.
    """
    if not sorted_list:
        return 0.0
    index = (len(sorted_list) - 1) * percentile
    lower = int(index)
    upper = lower + 1
    if upper < len(sorted_list):
        return sorted_list[lower] + (sorted_list[upper] - sorted_list[lower]) * (index - lower)
    return sorted_list[lower]


async def main():
    parser = argparse.ArgumentParser(description="Simulador de Carga Concorrente e Debounce para Webhook WhatsApp (Melhorado)")
    parser.add_argument("--url", default="http://localhost:8000", help="URL base do servidor FastAPI")
    parser.add_argument("--tenant", default="org_debug", help="ID do Tenant a ser testado")
    parser.add_argument("--phones", type=int, default=10, help="Quantidade de números concorrentes")
    parser.add_argument("--messages", type=int, default=3, help="Quantidade de mensagens rápidas por número")
    parser.add_argument("--debounce-wait", type=int, default=35, help="Tempo a esperar em segundos para processamento do debounce (deve ser > Settings.debounce_seconds)")
    args = parser.parse_args()

    # Enforce ENCRYPTION_KEY check for database validation
    if not os.environ.get("ENCRYPTION_KEY"):
        print("[WARNING] A variável de ambiente ENCRYPTION_KEY não está definida.")
        print("          A verificação direta do banco de dados do tenant poderá falhar se o connection string estiver criptografado.")

    print(f"=== Iniciando Simulação de Carga Concorrente ===")
    print(f"URL Alvo: {args.url}")
    print(f"Tenant: {args.tenant}")
    print(f"Número de Telefones Concorrentes: {args.phones}")
    print(f"Mensagens rápidas por Telefone: {args.messages} (intervalo de 0.5s)")
    print(f"Total de webhooks a serem enviados: {args.phones * args.messages}")

    start_time = time.perf_counter()
    latencies, simulated_phones = await run_load(args.url, args.tenant, args.phones, args.messages)
    end_time = time.perf_counter()

    total_sent = len(latencies)
    if total_sent == 0:
        print("[ERROR] Nenhuma requisição enviada com sucesso.")
        sys.exit(1)

    # Sort latencies for percentile calculations
    sorted_lats = sorted(latencies)
    avg_latency = (sum(latencies) / total_sent) * 1000  # in ms
    min_latency = sorted_lats[0] * 1000  # in ms
    max_latency = sorted_lats[-1] * 1000  # in ms
    p95_latency = calculate_percentile(sorted_lats, 0.95) * 1000  # in ms
    p99_latency = calculate_percentile(sorted_lats, 0.99) * 1000  # in ms
    total_duration = end_time - start_time

    # SLA Check: individual request response times must be < 500ms
    sla_threshold_ms = 500.0
    sla_violations = [lat for lat in latencies if lat * 1000 >= sla_threshold_ms]
    sla_violation_count = len(sla_violations)
    sla_success_percent = ((total_sent - sla_violation_count) / total_sent) * 100

    print(f"\n=== Relatório de Latência de Envio (Meta SLA < 500ms) ===")
    print(f"Total de Webhooks Enviados: {total_sent}")
    print(f"Tempo Total de Execução de Envio: {total_duration:.2f}s")
    print(f"Tempo Mínimo: {min_latency:.2f}ms")
    print(f"Tempo Máximo: {max_latency:.2f}ms")
    print(f"Tempo Médio: {avg_latency:.2f}ms")
    print(f"P95 (95% das reqs abaixo de): {p95_latency:.2f}ms")
    print(f"P99 (99% das reqs abaixo de): {p99_latency:.2f}ms")
    print(f"Violações de SLA (> 500ms): {sla_violation_count} de {total_sent} ({sla_success_percent:.2f}% de sucesso)")

    latency_success = avg_latency < sla_threshold_ms
    if latency_success:
        print("✅ Média de tempo de resposta atende o critério de SLA (< 500ms)")
    else:
        print("❌ Média de tempo de resposta viola o critério de SLA (> 500ms)")

    # Wait for the debounce to finish and processing tasks to write database
    print(f"\nAguardando {args.debounce_wait}s para processamento do debounce de 30s...")
    await asyncio.sleep(args.debounce_wait)

    print("\n=== Verificação do Banco de Dados do Tenant ===")
    db_success = False
    try:
        db_status = await verify_database(args.tenant, simulated_phones)
        print(f"Mensagens restantes no buffer para os telefones testados: {db_status['buffer_count']} (Esperado: 0)")
        print(f"Mensagens totais restantes no buffer (geral): {db_status['total_buffer_count']} (Esperado: 0)")
        print(f"Clientes persistidos correspondentes ao teste: {db_status['registered_count']} de {args.phones} (Esperado: {args.phones})")
        
        if db_status["missing_phones"]:
            print(f"❌ Telefones não registrados no banco: {db_status['missing_phones']}")
        else:
            print("✅ Todos os telefones de teste foram registrados com sucesso no banco de dados!")
            
        db_success = db_status["success"]
        if db_success:
            print("✅ Sucesso de consolidação e limpeza do buffer verificado no banco!")
        else:
            print("❌ Falha: mensagens pendentes no buffer ou inconsistência no registro de clientes.")
    except Exception as e:
        print(f"⚠️ Não foi possível verificar o banco do tenant: {e}")
        print("Nota: Certifique-se de rodar com as variáveis de ambiente corretas (ENCRYPTION_KEY e DATABASE_URL).")

    # Final overall script assertion
    if latency_success and db_success:
        print("\n🎉 SIMULAÇÃO BEM-SUCEDIDA: SLA de latência atendido e consolidação de dados verificada.")
        sys.exit(0)
    else:
        print("\n🚨 SIMULAÇÃO COM FALHAS: Verifique as violações acima.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
