import asyncio
import logging
import zoneinfo
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select

from app.core.database import SessionLocal as default_sessionmaker
from app.models.settings import Settings
from app.core.tenant_database import tenant_db_manager
from app.models.agent_config import AgentConfig
from app.services.session_manager import session_manager
from app.services.medflow_client import MedflowClient
from app.services.whatsapp_client import whatsapp_client
from app.services.agents.graph import get_llm_from_config
from langchain_core.messages import SystemMessage, HumanMessage

logger = logging.getLogger(__name__)

# Module-level variable to hold the background task reference
_followup_task: Optional[asyncio.Task] = None


async def check_and_send_followups(central_sessionmaker=None) -> None:
    """
    Checks all active tenants, queries appointments from yesterday,
    and sends automatic follow-ups or no-show messages to patients.
    """
    if central_sessionmaker is None:
        central_sessionmaker = default_sessionmaker

    # 1. Establish America/Sao_Paulo timezone
    try:
        tz = zoneinfo.ZoneInfo("America/Sao_Paulo")
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    now_tz = datetime.now(tz)
    yesterday = now_tz.date() - timedelta(days=1)
    yesterday_str = yesterday.isoformat()

    # 2. Query all settings from the central database to find organization_ids
    try:
        async with central_sessionmaker() as session:
            stmt = select(Settings)
            result = await session.execute(stmt)
            settings_records = result.scalars().all()
            org_ids = [s.organization_id for s in settings_records]
    except Exception as e:
        logger.error(f"Failed to query organizations for follow-ups: {e}", exc_info=True)
        return

    redis_client = await session_manager.get_client()

    for org_id in org_ids:
        try:
            # 3. Load active AgentConfig for agent_type = 'followup'
            async with await tenant_db_manager.get_tenant_session(org_id) as tenant_session:
                stmt = select(AgentConfig).where(
                    AgentConfig.agent_type == "followup",
                    AgentConfig.is_active == True
                )
                res = await tenant_session.execute(stmt)
                config = res.scalar_one_or_none()

            if not config:
                logger.debug(f"No active follow-up configuration found for tenant {org_id}")
                continue

            # 4. Fetch CRM appointments for yesterday using placeholder doctor ID
            client = MedflowClient(tenant_id=org_id)
            appointments = await client.get_crm_appointments(
                date=yesterday_str,
                doctor_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
                tenant_id=org_id
            )

            for appt in appointments:
                try:
                    patient_name = appt.get("patientName") or appt.get("patient_name")
                    phone_number = appt.get("patientPhone") or appt.get("patient_phone") or appt.get("phone")
                    appt_id = appt.get("id") or appt.get("appointmentId")
                    appt_time = appt.get("time") or "00:00"
                    procedure = appt.get("procedure")
                    appt_status = appt.get("status")

                    if not phone_number:
                        logger.warning(f"No phone number for appointment {appt_id} in tenant {org_id}")
                        continue

                    # Determine if appointment is eligible and if patient was a no-show
                    if appt_status is None:
                        continue

                    # Clean spaces, hyphens, underscores, and lowercases before comparison
                    status_str = str(appt_status).lower().replace(" ", "").replace("-", "").replace("_", "")

                    is_atendido = any(x in status_str for x in ["atendido", "compareceu", "realizado"])
                    is_faltou = any(x in status_str for x in ["naoveio", "faltou", "ausente", "noshow"])

                    if is_atendido and ("nao" in status_str or "não" in status_str):
                        is_noshow = True
                        is_atendido = False
                    elif is_atendido:
                        is_noshow = False
                    elif is_faltou:
                        is_noshow = True
                    else:
                        continue

                    # 5. Redis duplicate check per appointment/patient-date
                    if appt_id:
                        dup_key = f"followup_sent:{org_id}:{appt_id}"
                    else:
                        dup_key = f"followup_sent:{org_id}:{phone_number}:{yesterday_str}"

                    already_sent = await redis_client.get(dup_key)
                    if already_sent:
                        logger.info(f"Follow-up already sent for key {dup_key}. Skipping.")
                        continue

                    # 6. Resolve prompts
                    if is_noshow:
                        system_prompt = config.system_prompt_noshow or (
                            "Você é um assistente de clínica médica encarregado de enviar mensagens "
                            "gentis de acompanhamento para pacientes que faltaram à consulta, "
                            "perguntando se desejam reagendar."
                        )
                    else:
                        system_prompt = config.system_prompt or (
                            "Você é um assistente de clínica médica encarregado de enviar mensagens "
                            "gentis de acompanhamento pós-consulta para obter feedback do paciente."
                        )

                    human_content = f"Paciente: {patient_name}. Data da consulta: {yesterday_str} às {appt_time}."
                    if procedure:
                        human_content += f" Procedimento: {procedure}."

                    # 7. LLM invocation with strict fallback
                    try:
                        llm = get_llm_from_config(config)
                        messages = [
                            SystemMessage(content=system_prompt),
                            HumanMessage(content=human_content)
                        ]
                        response = await llm.ainvoke(messages)
                        text = response.content
                    except Exception as llm_err:
                        logger.warning(f"LLM generation failed for tenant {org_id}, using fallback: {llm_err}")
                        if is_noshow:
                            text = (
                                f"Olá, {patient_name}. Sentimos sua falta na consulta de ontem "
                                f"({yesterday_str} às {appt_time}). Gostaria de reagendar o seu "
                                f"atendimento? Por favor, nos avise por aqui."
                            )
                        else:
                            text = (
                                f"Olá, {patient_name}. Esperamos que tenha corrido tudo bem em sua consulta "
                                f"de ontem ({yesterday_str} às {appt_time}). Como você está se sentindo? "
                                f"Sua opinião é muito importante para nós."
                            )

                    # 8. Send WhatsApp Message
                    await whatsapp_client.send_message(
                        phone_number=phone_number,
                        text=text,
                        organization_id=org_id
                    )

                    # 9. Persist sent state in Redis (30 days TTL to cover retries/reschedules)
                    await redis_client.set(dup_key, "1", ex=30 * 24 * 3600)

                except Exception as appt_err:
                    logger.error(
                        f"Failed to process individual follow-up in tenant {org_id}: {appt_err}",
                        exc_info=True
                    )
        except Exception as tenant_err:
            logger.error(f"Critical error processing follow-ups for tenant {org_id}: {tenant_err}", exc_info=True)


async def run_followup_loop(sleep_interval: float = 600.0) -> None:
    """
    Asynchronous background loop task that runs check_and_send_followups.
    Defaults to 10 minutes (600 seconds) sleep interval.
    """
    logger.info("Automatic follow-ups loop started.")
    while True:
        try:
            await check_and_send_followups()
        except asyncio.CancelledError:
            logger.info("Automatic follow-ups loop cancelled.")
            raise
        except Exception as loop_err:
            logger.error(f"Exception in automatic follow-ups loop: {loop_err}", exc_info=True)
        try:
            await asyncio.sleep(sleep_interval)
        except asyncio.CancelledError:
            logger.info("Automatic follow-ups loop sleep cancelled.")
            raise


async def start_followup_task(sleep_interval: float = 600.0) -> None:
    """
    Starts the background follow-ups loop.
    """
    global _followup_task
    if _followup_task is None or _followup_task.done():
        _followup_task = asyncio.create_task(run_followup_loop(sleep_interval))
        logger.info("Automatic follow-ups background task started.")


async def stop_followup_task() -> None:
    """
    Cancels and waits for the background follow-ups loop.
    """
    global _followup_task
    if _followup_task is not None:
        _followup_task.cancel()
        try:
            await _followup_task
        except asyncio.CancelledError:
            pass
        _followup_task = None
        logger.info("Automatic follow-ups background task stopped.")
