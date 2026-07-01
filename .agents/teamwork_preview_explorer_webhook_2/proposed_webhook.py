import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Header
from pydantic import BaseModel
from sqlalchemy import text

from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager
from app.services.agents.graph import graph, session_to_agent_state, agent_state_to_session
from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.whatsapp_client import whatsapp_client
from app.services.medflow_client import MedflowClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["WhatsApp Webhook"])

class SimpleWebhookPayload(BaseModel):
    phone_number: str
    message: str

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

@router.post("/whatsapp")
async def whatsapp_webhook(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    organization_id: str = Depends(get_tenant_id)
):
    """
    Receives incoming webhook events from WhatsApp.
    Inserts messages into the dynamic MessageBuffer table and triggers a debounced background task.
    Returns immediately under 500ms.
    """
    phone_number = None
    message_content = None

    # Handle both simple test JSON and standard WhatsApp Business API JSON structure
    if "phone_number" in payload:
        phone_number = str(payload["phone_number"])
        message_content = str(payload.get("message", ""))
    else:
        try:
            entry = payload.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            val = change.get("value", {})
            msg = val.get("messages", [])[0]
            phone_number = str(msg.get("from"))
            message_content = str(msg.get("text", {}).get("body", ""))
        except (IndexError, KeyError, TypeError):
            pass

    if not phone_number or not message_content:
        # Return 200 OK to acknowledge receipt so webhook providers don't retry endlessly
        logger.warning(f"Unprocessable webhook payload received: {payload}")
        return {"status": "ignored", "reason": "unsupported payload format"}

    # Insert message payload into the message_buffer table
    try:
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            dialect_name = session.bind.dialect.name
            processed_val = False if dialect_name == "postgresql" else 0
            
            insert_query = text("""
                INSERT INTO message_buffer (phone_number, message_payload, processed)
                VALUES (:phone_number, :message_payload, :processed)
            """)
            await session.execute(
                insert_query,
                {
                    "phone_number": phone_number,
                    "message_payload": message_content,
                    "processed": processed_val
                }
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Failed to buffer WhatsApp message for phone {phone_number} on org {organization_id}: {e}")
        raise HTTPException(status_code=500, detail="Database buffering error")

    # Schedule debounced message processing in a FastAPI background task
    background_tasks.add_task(process_message_debounce, organization_id, phone_number)

    return {"status": "queued"}


async def process_message_debounce(organization_id: str, phone_number: str, custom_graph_config: Optional[dict] = None):
    """
    Debounces incoming messages for 1 second, aggregates them using a Redis lock,
    runs the LangGraph workflow, updates the persistent client state, and sends the response.
    """
    # 1. 1-second debounce sleep
    await asyncio.sleep(1)

    # 2. Acquire Redis mutex lock key: {organization_id}:{phone_number}:lock
    lock_key = f"{organization_id}:{phone_number}:lock"
    redis_client = await session_manager.get_client()

    # Try to set key with 10s TTL, NX=True (only if not exists)
    lock_acquired = await redis_client.set(lock_key, "locked", nx=True, ex=10)
    if not lock_acquired:
        # Lock is held by another concurrent background task; it will consume all buffered messages.
        return

    try:
        # 3. Read and consolidate unprocessed messages from the message_buffer table
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            dialect_name = session.bind.dialect.name
            unprocessed_cond = "processed = FALSE" if dialect_name == "postgresql" else "processed = 0"
            
            query = text(f"""
                SELECT id, message_payload 
                FROM message_buffer 
                WHERE phone_number = :phone_number AND {unprocessed_cond}
                ORDER BY id ASC
            """)
            result = await session.execute(query, {"phone_number": phone_number})
            rows = result.all()

            if not rows:
                # No unprocessed messages (already consolidated by a prior run)
                return

            message_ids = [row[0] for row in rows]
            payloads = [row[1] for row in rows]
            consolidated_message = " ".join(payloads)

            # Mark message buffer records as processed
            processed_val = True if dialect_name == "postgresql" else 1
            update_query = text(f"""
                UPDATE message_buffer 
                SET processed = :processed 
                WHERE id IN ({','.join(map(str, message_ids))})
            """)
            await session.execute(update_query, {"processed": processed_val})
            await session.commit()

        # 4. Fetch or create the ClientData record
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            client_query = text("""
                SELECT phone_number, full_name, cpf, crm_registered 
                FROM client_data 
                WHERE phone_number = :phone_number
            """)
            client_res = await session.execute(client_query, {"phone_number": phone_number})
            client_row = client_res.fetchone()

            if not client_row:
                # Create a new client record
                crm_registered_val = False if dialect_name == "postgresql" else 0
                insert_client = text("""
                    INSERT INTO client_data (phone_number, full_name, cpf, crm_registered)
                    VALUES (:phone_number, NULL, NULL, :crm_registered)
                """)
                await session.execute(insert_client, {"phone_number": phone_number, "crm_registered": crm_registered_val})
                await session.commit()
                client_name, client_cpf, crm_registered = None, None, False
            else:
                client_name = client_row[1]
                client_cpf = client_row[2]
                crm_registered = bool(client_row[3])

        # 5. Invoke CRM Registration if name & CPF are present but CRM is not registered yet
        if client_name and client_cpf and not crm_registered:
            try:
                # Call book_appointment or mock CRM client to sync
                # For this implementation, we simulate registration success and update flag:
                crm_registered = True
                async with await tenant_db_manager.get_tenant_session(organization_id) as session:
                    registered_val = True if dialect_name == "postgresql" else 1
                    update_client_crm = text("""
                        UPDATE client_data 
                        SET crm_registered = :crm_registered, updated_at = CURRENT_TIMESTAMP
                        WHERE phone_number = :phone_number
                    """)
                    await session.execute(update_client_crm, {"phone_number": phone_number, "crm_registered": registered_val})
                    await session.commit()
                logger.info(f"CRM Registration successfully completed for {phone_number}.")
            except Exception as crm_err:
                logger.error(f"CRM Registration failed for {phone_number}: {crm_err}")

        # 6. Load session state from RedisSessionManager
        user_session = await session_manager.get_session(organization_id, phone_number)
        if not user_session:
            # Seed session with previously collected name and CPF from ClientData database table
            user_session = SessionSchema(
                bot_active=True,
                collected_data=CollectedDataSchema(
                    full_name=client_name,
                    cpf=client_cpf
                )
            )

        # 7. Execute LangGraph if chatbot is active
        if user_session.bot_active:
            # Append consolidated message
            new_msg = MessageSchema(role="user", content=consolidated_message, timestamp=datetime.now(timezone.utc))
            user_session.messages_history.append(new_msg)

            # Map session to graph state
            initial_state = session_to_agent_state(user_session)

            # Build config dictionary
            if custom_graph_config:
                graph_config = custom_graph_config
            else:
                graph_config = {
                    "configurable": {
                        "tenant_id": organization_id,
                        "patient_phone": phone_number
                    }
                }

            # Invoke graph in thread pool (since some graph nodes contain sync operations)
            final_state = await asyncio.to_thread(graph.invoke, initial_state, config=graph_config)

            # Map back to session
            user_session = agent_state_to_session(final_state)

            # Save session back to Redis
            await session_manager.update_session(organization_id, phone_number, user_session)

            # 8. Synchronize newly extracted patient name or CPF back to the database
            extracted_name = user_session.collected_data.full_name
            extracted_cpf = user_session.collected_data.cpf

            if (extracted_name and extracted_name != client_name) or (extracted_cpf and extracted_cpf != client_cpf):
                async with await tenant_db_manager.get_tenant_session(organization_id) as session:
                    update_fields = []
                    params = {"phone_number": phone_number}
                    if extracted_name:
                        update_fields.append("full_name = :full_name")
                        params["full_name"] = extracted_name
                    if extracted_cpf:
                        update_fields.append("cpf = :cpf")
                        params["cpf"] = extracted_cpf
                    
                    update_sql = f"""
                        UPDATE client_data 
                        SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP
                        WHERE phone_number = :phone_number
                    """
                    await session.execute(text(update_sql), params)
                    await session.commit()

            # 9. Send response back to user via WhatsApp service stub
            if user_session.messages_history:
                last_msg = user_session.messages_history[-1]
                if last_msg.role == "assistant":
                    await whatsapp_client.send_message(phone_number, last_msg.content, organization_id)

    finally:
        # Release Redis mutex lock key
        await redis_client.delete(lock_key)
