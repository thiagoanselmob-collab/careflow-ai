import asyncio
import logging
import zoneinfo
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import select
from langchain_core.messages import SystemMessage, HumanMessage

from app.core.database import SessionLocal as default_sessionmaker
from app.models.settings import Settings
from app.core.tenant_database import tenant_db_manager
from app.models.agent_config import AgentConfig
from app.services.session_manager import session_manager
from app.services.medflow_client import MedflowClient
from app.services.whatsapp_client import whatsapp_client
from app.services.agents.graph import get_llm_from_config

logger = logging.getLogger(__name__)

# Module-level variable to hold the background task reference
_reminders_task: Optional[asyncio.Task] = None


async def check_and_send_reminders(central_sessionmaker=None) -> None:
    """
    Checks all active tenants, checks if reminder time matches current Sao Paulo time,
    and sends automatic reminders for their appointments.
    """
    if central_sessionmaker is None:
        central_sessionmaker = default_sessionmaker

    # Get current time in Sao Paulo timezone
    try:
        tz = zoneinfo.ZoneInfo("America/Sao_Paulo")
    except Exception:
        # Fallback if America/Sao_Paulo is not in zoneinfo database
        tz = zoneinfo.ZoneInfo("UTC")

    now_tz = datetime.now(tz)
    current_time_str = now_tz.strftime("%H:%M")
    current_date_str = now_tz.strftime("%Y-%m-%d")

    # 1. Query all settings from the central database to find organization_ids
    try:
        async with central_sessionmaker() as session:
            stmt = select(Settings)
            result = await session.execute(stmt)
            settings_records = result.scalars().all()
            org_ids = [s.organization_id for s in settings_records]
    except Exception as e:
        logger.error(f"Failed to query organizations from central database: {e}", exc_info=True)
        return

    redis_client = await session_manager.get_client()

    for org_id in org_ids:
        lock_key = f"reminder_sent:{org_id}:{current_date_str}"
        lock_acquired = False
        try:
            # Load active AgentConfig for agent_type = 'reminders'
            async with await tenant_db_manager.get_tenant_session(org_id) as tenant_session:
                stmt = select(AgentConfig).where(
                    AgentConfig.agent_type == "reminders",
                    AgentConfig.is_active == True
                )
                res = await tenant_session.execute(stmt)
                config = res.scalar_one_or_none()

            if not config:
                logger.debug(f"No active reminders configuration found for tenant {org_id}")
                continue

            # Check if current time in America/Sao_Paulo matches reminder_time
            if not config.reminder_time:
                logger.debug(f"No reminder_time set for tenant {org_id}")
                continue

            if current_time_str != config.reminder_time:
                logger.debug(
                    f"Current time {current_time_str} does not match reminder_time {config.reminder_time} for tenant {org_id}"
                )
                continue

            # Redis deduplication lock checks key exists, sets key
            already_sent = await redis_client.get(lock_key)
            if already_sent:
                logger.info(f"Reminders already sent for tenant {org_id} on {current_date_str}")
                continue

            # Lock set BEFORE starting dispatch (25-hour TTL)
            await redis_client.set(lock_key, "1", ex=25 * 3600)
            lock_acquired = True

            # Parse reminder_rules as list/JSON of integer offsets
            rules = config.reminder_rules
            if not rules:
                logger.info(f"No reminder_rules configured for tenant {org_id}")
                continue

            if isinstance(rules, str):
                try:
                    offsets = json.loads(rules)
                except Exception as parse_err:
                    logger.error(f"Failed to parse reminder_rules JSON '{rules}' for tenant {org_id}: {parse_err}")
                    continue
            elif isinstance(rules, list):
                offsets = rules
            else:
                logger.warning(f"Unexpected reminder_rules type for tenant {org_id}: {type(rules)}")
                continue

            if not isinstance(offsets, list):
                logger.warning(f"Parsed reminder_rules is not a list for tenant {org_id}: {offsets}")
                continue

            # Process each offset X
            for X in offsets:
                target_date = now_tz.date() + timedelta(days=int(X))
                
                # Fetch CRM appointments (exceptions bubble up to abort tenant execution and clear lock)
                client = MedflowClient(tenant_id=org_id)
                appointments = await client.get_crm_appointments(
                    date=target_date.isoformat(),
                    doctor_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    tenant_id=org_id
                )

                for appt in appointments:
                    try:
                        patient_name = appt.get("patientName") or appt.get("patient_name")
                        phone_number = appt.get("patientPhone") or appt.get("patient_phone") or appt.get("phone")
                        appt_date = appt.get("date")
                        appt_time = appt.get("time")
                        procedure = appt.get("procedure")

                        if not phone_number:
                            logger.warning(f"No phone number for appointment in tenant {org_id}")
                            continue

                        # Get LLM instance
                        llm = get_llm_from_config(config)

                        # Setup prompts
                        system_instruction = config.system_prompt or "Você é um assistente de clínica médica encarregado de enviar lembretes de consulta de forma gentil."
                        if int(X) == 1:
                            system_instruction += "\n\nObrigatório: Você DEVE finalizar a mensagem com a seguinte pergunta exata para que o paciente confirme: 'Responda SIM para confirmar ou NÃO para cancelar'."

                        human_content = f"Crie um lembrete para o paciente {patient_name} agendado para o dia {appt_date} às {appt_time}."
                        if procedure:
                            human_content += f" Procedimento: {procedure}."

                        messages = [
                            SystemMessage(content=system_instruction),
                            HumanMessage(content=human_content)
                        ]

                        response = await llm.ainvoke(messages)
                        text = response.content

                        # Ensure X==1 prompt compliance
                        if int(X) == 1:
                            if "Responda SIM para confirmar ou NÃO para cancelar" not in text:
                                text = text.strip() + "\n\nResponda SIM para confirmar ou NÃO para cancelar"

                        # Send WhatsApp message
                        await whatsapp_client.send_message(
                            phone_number=phone_number,
                            text=text,
                            organization_id=org_id
                        )
                    except Exception as appt_err:
                        logger.error(
                            f"Failed to send reminder for appointment in tenant {org_id}: {appt_err}",
                            exc_info=True
                        )
        except Exception as tenant_err:
            logger.error(f"Critical error processing reminders for tenant {org_id}: {tenant_err}", exc_info=True)
            # Lock deleted if critical error aborts tenant's execution
            if lock_acquired:
                try:
                    await redis_client.delete(lock_key)
                    logger.info(f"Deleted lock key {lock_key} due to critical tenant abort.")
                except Exception as redis_err:
                    logger.error(f"Failed to delete lock key {lock_key} after tenant abort: {redis_err}")


async def run_reminders_loop(sleep_interval: float = 60.0) -> None:
    """
    Asynchronous background loop task that runs check_and_send_reminders continuously.
    """
    logger.info("Automatic reminders loop started.")
    while True:
        try:
            await check_and_send_reminders()
        except asyncio.CancelledError:
            logger.info("Automatic reminders loop cancelled.")
            raise
        except Exception as loop_err:
            logger.error(f"Exception in automatic reminders loop: {loop_err}", exc_info=True)
        try:
            await asyncio.sleep(sleep_interval)
        except asyncio.CancelledError:
            logger.info("Automatic reminders loop sleep cancelled.")
            raise


async def start_reminders_task(sleep_interval: float = 60.0) -> None:
    """
    Controls starting the background reminders task.
    """
    global _reminders_task
    if _reminders_task is None or _reminders_task.done():
        _reminders_task = asyncio.create_task(run_reminders_loop(sleep_interval))
        logger.info("Automatic reminders background task started.")


async def stop_reminders_task() -> None:
    """
    Controls stopping the background reminders task.
    """
    global _reminders_task
    if _reminders_task is not None:
        _reminders_task.cancel()
        try:
            await _reminders_task
        except asyncio.CancelledError:
            pass
        _reminders_task = None
        logger.info("Automatic reminders background task stopped.")
