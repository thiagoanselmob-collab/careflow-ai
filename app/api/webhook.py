import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Header
from sqlalchemy import text

from app.core.tenant_database import tenant_db_manager
from app.services.session_manager import session_manager
from app.services.agents.graph import graph, session_to_agent_state, agent_state_to_session
from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from app.services.whatsapp_client import whatsapp_client
from app.services.medflow_client import MedflowClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["WhatsApp Webhook"])


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
    # Check if this is a WhatsApp status update
    is_status_update = False
    if "statuses" in payload:
        is_status_update = True
    else:
        try:
            entry = payload.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            val = change.get("value", {})
            if "statuses" in val:
                is_status_update = True
        except (IndexError, KeyError, TypeError):
            pass

    if is_status_update:
        return {"status": "ignored", "reason": "status update"}

    # Determine if outgoing from clinic (fromMe)
    is_from_me = False
    if payload.get("fromMe") is True:
        is_from_me = True

    phone_number = None
    message_content = None

    # Handle both simple test JSON and standard WhatsApp Business API JSON structure
    if "phone_number" in payload:
        phone_number = str(payload["phone_number"])
        message_content = str(payload.get("content") or payload.get("message") or "")
    else:
        try:
            entry = payload.get("entry", [])[0]
            change = entry.get("changes", [])[0]
            val = change.get("value", {})
            msg = val.get("messages", [])[0]
            if msg.get("fromMe") is True:
                is_from_me = True
            # For outgoing messages, msg.get("to") is the patient's phone number
            phone_number = str(msg.get("to") if is_from_me else msg.get("from"))
            if not phone_number or phone_number == "None":
                phone_number = str(msg.get("from"))
            message_content = str(msg.get("text", {}).get("body", ""))
        except (IndexError, KeyError, TypeError):
            pass

    if not phone_number or not message_content:
        logger.warning(f"Unprocessable webhook payload received: {payload}")
        return {"status": "ignored", "reason": "unsupported payload format"}

    if is_from_me:
        # Check Redis for the bot_sending key
        redis_client = await session_manager.get_client()
        bot_sending_key = f"bot_sending:{organization_id}:{phone_number}"
        bot_sending_exists = await redis_client.exists(bot_sending_key)
        
        if bot_sending_exists:
            return {"status": "ignored", "reason": "bot self-reply"}
        else:
            # Human takeover detected!
            try:
                async with await tenant_db_manager.get_tenant_session(organization_id) as session:
                    update_query = text("""
                        UPDATE dados_cliente 
                        SET status = 'ATENDIMENTO_HUMANO'
                        WHERE phone_number = :phone_number
                    """)
                    await session.execute(update_query, {"phone_number": phone_number})
                    await session.commit()
            except Exception as e:
                logger.error(f"Failed to update client status to ATENDIMENTO_HUMANO in webhook: {e}")
                
            try:
                user_session = await session_manager.get_session(organization_id, phone_number)
                if not user_session:
                    user_session = SessionSchema(
                        bot_active=False,
                        collected_data=CollectedDataSchema(),
                        wants_to_schedule=False
                    )
                else:
                    user_session.bot_active = False
                await session_manager.update_session(organization_id, phone_number, user_session)
            except Exception as e:
                logger.error(f"Failed to deactivate bot in session: {e}")
                
            return {"status": "ignored", "reason": "human takeover detected"}


    # Insert message payload into the message_buffer table
    try:
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            insert_query = text("""
                INSERT INTO message_buffer (phone_number, content)
                VALUES (:phone_number, :content)
            """)
            await session.execute(
                insert_query,
                {
                    "phone_number": phone_number,
                    "content": message_content
                }
            )
            await session.commit()
            
            import time
            redis_client = await session_manager.get_client()
            last_msg_time_key = f"last_msg_time:{organization_id}:{phone_number}"
            await redis_client.set(last_msg_time_key, str(time.time()))
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
    # 1. Debounce sleep using config settings
    from app.core.config import settings
    await asyncio.sleep(settings.debounce_seconds)

    redis_client = await session_manager.get_client()
    last_msg_time_key = f"last_msg_time:{organization_id}:{phone_number}"
    last_msg_time_val = await redis_client.get(last_msg_time_key)

    if last_msg_time_val:
        import time
        last_msg_time = float(last_msg_time_val)
        current_time = time.time()
        if current_time - last_msg_time < settings.debounce_seconds:
            # Exit silently as a newer message reset the debounce
            return

    # 2. Acquire Redis mutex lock key: {organization_id}:{phone_number}:lock
    lock_key = f"{organization_id}:{phone_number}:lock"
    import uuid
    lock_value = str(uuid.uuid4())

    # Try to set key with 60s TTL, NX=True, with retries to prevent race conditions during exit release
    lock_acquired = False
    for _ in range(5):
        lock_acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=60)
        if lock_acquired:
            break
        await asyncio.sleep(0.1)

    if not lock_acquired:
        # Lock is held by another concurrent background task; it will consume all buffered messages.
        return

    try:
        while True:
            should_register_crm = False
            consolidated_message = ""

            # 3. Read, consolidate, delete messages and check client status in a single DB session
            async with await tenant_db_manager.get_tenant_session(organization_id) as session:
                query = text("""
                    SELECT id, content 
                    FROM message_buffer 
                    WHERE phone_number = :phone_number
                    ORDER BY id ASC
                """)
                result = await session.execute(query, {"phone_number": phone_number})
                rows = result.all()

                if not rows:
                    # No messages in buffer, break the loop
                    break

                message_ids = [row[0] for row in rows]
                payloads = [row[1] for row in rows]
                consolidated_message = "\n".join(payloads)

                # Parameterize the deletion query to prevent SQL Injection smells
                from sqlalchemy import bindparam
                delete_query = text("DELETE FROM message_buffer WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                )
                await session.execute(delete_query, {"ids": message_ids})

                # 4. Fetch or create the ClientData record in dados_cliente table
                client_query = text("""
                    SELECT phone_number, status 
                    FROM dados_cliente 
                    WHERE phone_number = :phone_number
                """)
                client_res = await session.execute(client_query, {"phone_number": phone_number})
                client_row = client_res.fetchone()

                if not client_row:
                    # Create a new client record
                    insert_client = text("""
                        INSERT INTO dados_cliente (phone_number, status)
                        VALUES (:phone_number, 'EM_CONTATO')
                    """)
                    await session.execute(insert_client, {"phone_number": phone_number})
                    should_register_crm = True

                await session.commit()

            # 5. Invoke CRM Registration via MedflowClient if new client was registered
            original_appt_id = None
            if should_register_crm:
                medflow_client = MedflowClient(tenant_id=organization_id)
                current_time = datetime.now(timezone.utc)
                try:
                    crm_res = await medflow_client.book_appointment(
                        doctor_id="default_doctor",
                        date=current_time.strftime("%Y-%m-%d"),
                        time=current_time.strftime("%H:%M"),
                        patient_name="WhatsApp Client",
                        patient_phone=phone_number,
                        tenant_id=organization_id
                    )
                    logger.info(f"CRM Registration successfully completed for {phone_number}. Result: {crm_res}")
                    if isinstance(crm_res, dict):
                        original_appt_id = crm_res.get("appointmentId") or crm_res.get("id")
                        if not original_appt_id and "appointment" in crm_res:
                            appt_data = crm_res["appointment"]
                            if isinstance(appt_data, dict):
                                original_appt_id = appt_data.get("id") or appt_data.get("appointmentId")
                except Exception as crm_err:
                    logger.error(f"CRM Registration failed for {phone_number}: {crm_err}")

            # 6. Load session state from RedisSessionManager
            user_session = await session_manager.get_session(organization_id, phone_number)
            if not user_session:
                user_session = SessionSchema(
                    bot_active=True,
                    collected_data=CollectedDataSchema(),
                    wants_to_schedule=False
                )
            
            if original_appt_id:
                user_session.original_appointment_id = original_appt_id
                await session_manager.update_session(organization_id, phone_number, user_session)


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

                # 8. Send response back to user via WhatsApp service stub
                if user_session.messages_history:
                    last_msg = user_session.messages_history[-1]
                    if last_msg.role == "assistant":
                        await whatsapp_client.send_message(phone_number, last_msg.content, organization_id)

    finally:
        # Release Redis mutex lock key using safe Lua script
        lua_release = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            await redis_client.eval(lua_release, 1, lock_key, lock_value)
        except Exception:
            # Fallback for fakeredis without Lua support in tests
            if await redis_client.get(lock_key) == lock_value:
                await redis_client.delete(lock_key)
