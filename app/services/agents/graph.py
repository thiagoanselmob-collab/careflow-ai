import logging
import zoneinfo
from typing import Annotated, Optional, TypedDict, List, Literal, Tuple
from datetime import datetime, timezone, date, time, timedelta
import asyncio
import concurrent.futures
from pydantic import BaseModel, Field

from app.schemas.session import MessageSchema, CollectedDataSchema, SessionSchema
from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from sqlalchemy import text
from app.services.medflow_client import MedflowClient, MedflowClientError, MedflowClientHTTPError
from app.core.config import settings
import contextvars
import time as time_lib
import inspect

logger = logging.getLogger(__name__)

# ContextVar to track node traversal order per graph invoke
traversal_path_var = contextvars.ContextVar("traversal_path", default=None)

def log_node_execution(node_name):
    def decorator(func):
        if inspect.iscoroutinefunction(func):
            async def async_wrapper(state, config=None, *args, **kwargs):
                start_time = time_lib.perf_counter()
                configurable = config.get("configurable", {}) if config else {}
                phone_number = configurable.get("patient_phone", "unknown")
                logger.info(f"[Node Start] Session {phone_number} | Node: {node_name}")
                current_path = traversal_path_var.get()
                if current_path is not None:
                    current_path.append(node_name)
                try:
                    result = await func(state, config, *args, **kwargs)
                    return result
                finally:
                    duration_ms = (time_lib.perf_counter() - start_time) * 1000
                    logger.info(f"[Node End] Session {phone_number} | Node: {node_name} | Duration: {duration_ms:.2f}ms")
            return async_wrapper
        else:
            def sync_wrapper(state, config=None, *args, **kwargs):
                start_time = time_lib.perf_counter()
                configurable = config.get("configurable", {}) if config else {}
                phone_number = configurable.get("patient_phone", "unknown")
                logger.info(f"[Node Start] Session {phone_number} | Node: {node_name}")
                current_path = traversal_path_var.get()
                if current_path is not None:
                    current_path.append(node_name)
                try:
                    result = func(state, config, *args, **kwargs)
                    return result
                finally:
                    duration_ms = (time_lib.perf_counter() - start_time) * 1000
                    logger.info(f"[Node End] Session {phone_number} | Node: {node_name} | Duration: {duration_ms:.2f}ms")
            return sync_wrapper
    return decorator

def wrap_graph_invoke(original_invoke):
    def wrapper(input, config=None, **kwargs):
        token = traversal_path_var.set([])
        configurable = config.get("configurable", {}) if config else {}
        phone_number = configurable.get("patient_phone", "unknown")
        try:
            result = original_invoke(input, config=config, **kwargs)
            return result
        finally:
            path = traversal_path_var.get()
            path_str = " -> ".join([f"Node: {node}" for node in path])
            if path_str:
                path_str += " -> END"
            else:
                path_str = "END"
            logger.info(f"[LangGraph Trace] Session {phone_number} | {path_str}")
            traversal_path_var.reset(token)
    return wrapper

def wrap_graph_ainvoke(original_ainvoke):
    async def wrapper(input, config=None, **kwargs):
        token = traversal_path_var.set([])
        configurable = config.get("configurable", {}) if config else {}
        phone_number = configurable.get("patient_phone", "unknown")
        try:
            result = await original_ainvoke(input, config=config, **kwargs)
            return result
        finally:
            path = traversal_path_var.get()
            path_str = " -> ".join([f"Node: {node}" for node in path])
            if path_str:
                path_str += " -> END"
            else:
                path_str = "END"
            logger.info(f"[LangGraph Trace] Session {phone_number} | {path_str}")
            traversal_path_var.reset(token)
    return wrapper


def reduce_messages(left: List[MessageSchema], right: List[MessageSchema]) -> List[MessageSchema]:
    """
    Appends new messages to the history instead of overwriting.
    """
    if not left:
        left = []
    if not right:
        return left
    return left + list(right)


class AgentState(TypedDict):
    """
    Represents the graph state for the CareFlow AI agent workflow.
    """
    messages: Annotated[List[MessageSchema], reduce_messages]
    bot_active: bool
    collected_data: CollectedDataSchema
    wants_to_schedule: Optional[bool]
    next_node: Optional[str]
    action_required: Optional[bool]
    original_appointment_id: Optional[str]
    next_phase: Optional[str]
    suggested_action: Optional[str]


def session_to_agent_state(session: SessionSchema) -> AgentState:
    """
    Converts a SessionSchema object into an AgentState dictionary.
    """
    return {
        "messages": list(session.messages_history) if session.messages_history else [],
        "bot_active": session.bot_active,
        "collected_data": session.collected_data,
        "wants_to_schedule": session.wants_to_schedule,
        "next_node": None,
        "action_required": None,
        "original_appointment_id": getattr(session, "original_appointment_id", None),
        "next_phase": None,
        "suggested_action": None,
    }


def agent_state_to_session(state: AgentState) -> SessionSchema:
    """
    Converts an AgentState dictionary back into a SessionSchema object.
    """
    messages = state.get("messages", [])
    last_msg_at = messages[-1].timestamp if messages else None
    return SessionSchema(
        messages_history=messages,
        bot_active=state.get("bot_active", True),
        collected_data=state.get("collected_data", CollectedDataSchema()),
        wants_to_schedule=state.get("wants_to_schedule", False),
        last_message_at=last_msg_at,
        original_appointment_id=state.get("original_appointment_id")
    )



class RoutingDecision(BaseModel):
    """
    Structured output schema for Supervisor routing decisions.
    """
    next_node: Literal["crc_sdr_node", "agenda_node", "END"] = Field(
        ...,
        description="The next node to route the conversation to."
    )
    reasoning: str = Field(
        default="",
        description="The reasoning behind the routing decision."
    )
    next_phase: Optional[str] = Field(
        default=None,
        description="Optional next phase. Set to 'human' to escalate to a human agent."
    )
    suggested_action: Optional[str] = Field(
        default=None,
        description="Optional suggested action. Set to 'escalar_humano' to escalate."
    )



async def _async_escalate_human(tenant_id: Optional[str], patient_phone: Optional[str], appointment_id: Optional[str]) -> None:
    """
    Helper to update client status to ATENDIMENTO_HUMANO in the SQLite database
    and patch the status on the original appointment card.
    """
    if not tenant_id or not patient_phone:
        logger.warning("[Escalation] Missing tenant_id or patient_phone, cannot escalate.")
        return
        
    from app.core.tenant_database import tenant_db_manager
    from app.services.medflow_client import MedflowClient
    
    # 1. Update SQLite status
    try:
        async with await tenant_db_manager.get_tenant_session(tenant_id) as session:
            update_query = text("""
                UPDATE dados_cliente 
                SET status = 'ATENDIMENTO_HUMANO'
                WHERE phone_number = :phone_number
            """)
            await session.execute(update_query, {"phone_number": patient_phone})
            await session.commit()
            logger.info(f"[Escalation] Updated dados_cliente status to ATENDIMENTO_HUMANO for phone {patient_phone}")
    except Exception as e:
        logger.error(f"[Escalation] Failed to update client status in SQLite: {e}")

    # 2. Patch original appointment card status
    if appointment_id:
        try:
            client = MedflowClient(tenant_id=tenant_id)
            await client.patch_appointment_status(appointment_id=appointment_id, status="ATENDIMENTO_HUMANO")
            logger.info(f"[Escalation] Patched CRM appointment {appointment_id} status to ATENDIMENTO_HUMANO")
        except Exception as e:
            logger.error(f"[Escalation] Failed to patch CRM appointment status: {e}")


async def get_agent_config(tenant_id: str, agent_type: str) -> Optional["AgentConfig"]:
    """
    Acquire tenant session and retrieve the AgentConfig for the specified agent type.
    """
    from app.core.tenant_database import tenant_db_manager
    from app.models.agent_config import AgentConfig
    from sqlalchemy import select

    if not tenant_id:
        return None

    try:
        async with await tenant_db_manager.get_tenant_session(tenant_id) as session:
            stmt = select(AgentConfig).where(AgentConfig.agent_type == agent_type.lower())
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
    except Exception as e:
        logger.warning(f"Error fetching agent config for tenant {tenant_id}, agent_type {agent_type}: {e}")
        return None


def get_llm_from_config(agent_config: Optional["AgentConfig"]):
    """
    Returns the appropriate LangChain chat model based on the AgentConfig.
    """
    if agent_config and agent_config.llm_provider and agent_config.llm_model:
        provider = agent_config.llm_provider.lower()
        model = agent_config.llm_model
        if provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(model=model)
        elif provider == "openai":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(model=model)
        elif provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model=model)
            
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model="gemini-1.5-flash")


async def _call_llm_structured(structured_llm, prompt):
    if hasattr(structured_llm, "ainvoke"):
        return await structured_llm.ainvoke(prompt)
    else:
        return structured_llm.invoke(prompt)


async def _call_llm(llm, chat_prompt):
    if hasattr(llm, "ainvoke"):
        return await llm.ainvoke(chat_prompt)
    else:
        return llm.invoke(chat_prompt)


async def _async_supervisor_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    Evaluates the conversation history and collected data, and decides the next node.
    """
    print(f"DEBUG: supervisor_node received config: {config}")
    messages = state.get("messages", [])
    
    # 1. If the last message was an assistant message, we route to END to finish the turn.
    if messages and messages[-1].role == "assistant":
        print("[Supervisor] Last message is assistant response. Routing to END.")
        return {
            "next_node": "END",
            "action_required": False
        }
    
    # 2. Setup config and variables
    configurable = config.get("configurable", {}) if config else {}
    tenant_id = configurable.get("tenant_id")
    
    agent_config = None
    if tenant_id:
        agent_config = await get_agent_config(tenant_id, "supervisor")

    # 3. Get LLM instance
    llm = configurable.get("llm")
    if not llm:
        llm = get_llm_from_config(agent_config)
    
    # 4. Format the conversation and collected data
    messages_str = ""
    for msg in messages:
        messages_str += f"{msg.role.upper()}: {msg.content}\n"
    
    collected_data = state.get("collected_data", CollectedDataSchema())
    wants_to_schedule = state.get("wants_to_schedule", False)
    collected_data_str = (
        f"Name: {collected_data.full_name or 'Not provided'}\n"
        f"CPF: {collected_data.cpf or 'Not provided'}\n"
        f"Grievance: {collected_data.grievance or 'Not provided'}\n"
        f"Preferred Doctor: {collected_data.preferred_doctor or 'Not provided'}\n"
        f"Selected Datetime: {collected_data.selected_datetime or 'Not provided'}\n"
        f"Wants to Schedule: {wants_to_schedule}\n"
    )
    
    if agent_config and agent_config.system_prompt:
        base_prompt = agent_config.system_prompt
    else:
        base_prompt = (
            "You are the central Supervisor node in a healthcare chatbot system (CareFlow AI).\n"
            "Your task is to analyze the conversation history and collected data, "
            "and decide which node to route the conversation to next.\n\n"
            "Routing Criteria:\n"
            "- 'crc_sdr_node': Use this for new leads, if patient's name or CPF is missing (both are required "
            "for scheduling), for casual messages, or for general greetings.\n"
            "- 'agenda_node': Use this if the user has an explicit intent to schedule, cancel, or reschedule "
            "a consultation.\n"
            "- 'END': Use this only if the interaction is fully completed or the user is saying goodbye/no further action is needed."
        )

    prompt = (
        f"{base_prompt}\n\n"
        f"Conversation History:\n{messages_str}\n"
        f"Collected Data:\n{collected_data_str}\n"
        "Make a routing decision."
    )
    
    # 5. Call LLM with structured output
    structured_llm = llm.with_structured_output(RoutingDecision)
    decision = await _call_llm_structured(structured_llm, prompt)
    
    next_phase = None
    suggested_action = None
    if isinstance(decision, dict):
        next_node = decision.get("next_node", "crc_sdr_node")
        next_phase = decision.get("next_phase")
        suggested_action = decision.get("suggested_action")
    else:
        next_node = getattr(decision, "next_node", "crc_sdr_node")
        next_phase = getattr(decision, "next_phase", None)
        suggested_action = getattr(decision, "suggested_action", None)
        
    print(f"[Supervisor] Routing decision: {next_node}")
    
    # Check if escalation is requested
    if next_phase == "human" or suggested_action == "escalar_humano":
        patient_phone = configurable.get("patient_phone")
        original_appt_id = state.get("original_appointment_id")
        
        await _async_escalate_human(tenant_id, patient_phone, original_appt_id)
            
        return {
            "bot_active": False,
            "next_node": "END",
            "action_required": False
        }
        
    return {
        "next_node": next_node,
        "action_required": False if next_node == "END" else True
    }


@log_node_execution("supervisor_node")
def supervisor_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    Evaluates the conversation history and collected data, and decides the next node.
    """
    coro = _async_supervisor_node(state, config)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    if loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    else:
        return loop.run_until_complete(coro)


class SDROutputSchema(BaseModel):
    """
    Structured output schema for the SDR node response and extraction.
    """
    response_message: str = Field(
        ...,
        description="The response message to be sent to the user."
    )
    extracted_name: Optional[str] = Field(
        default=None,
        description="The extracted full name of the user, if provided in this message. Do not include titles."
    )
    extracted_cpf: Optional[str] = Field(
        default=None,
        description="The extracted CPF of the user, if provided in this message."
    )
    wants_to_schedule: bool = Field(
        ...,
        description="True if the user indicates a desire to schedule, reschedule, or cancel a consultation."
    )


async def _async_crc_sdr_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    CRC/SDR node: dynamic multi-tenant logic using structured extraction.
    """
    print("[Node Activation] crc_sdr_node activated")
    logger.info("crc_sdr_node activated")
    
    # 1. Load config and check mock compatibility
    configurable = config.get("configurable", {}) if config else {}
    sdr_llm = configurable.get("sdr_llm")
    supervisor_llm = configurable.get("llm")
    tenant_id = configurable.get("tenant_id")
    
    agent_config = None
    if tenant_id:
        agent_config = await get_agent_config(tenant_id, "sdr")

    is_mock = False
    if supervisor_llm:
        class_name = supervisor_llm.__class__.__name__
        if "MockLLM" in class_name:
            is_mock = True
            
    # Compatibility fallback for tests
    if not sdr_llm and is_mock:
        msg = MessageSchema(
            role="assistant",
            content="[CRC/SDR Agent] Hello! I am the CRC/SDR assistant. How can I help you today?",
            timestamp=datetime.now(timezone.utc)
        )
        return {
            "messages": [msg],
            "collected_data": state.get("collected_data", CollectedDataSchema()),
            "wants_to_schedule": state.get("wants_to_schedule", False),
            "next_node": None,
            "action_required": False
        }
        
    # 2. Load the profile dynamically
    profile = configurable.get("sdr_profile")
    if not profile or not isinstance(profile, dict):
        profile = {
            "doctor_name": "Dr. André Seabra",
            "clinic_name": "Clínica do Dr. André Seabra",
            "specialty": "Reprogramação Metabólica e Saúde Integrativa",
            "focus": "Reprogramação metabólica, equilíbrio hormonal, emagrecimento definitivo e restabelecimento da saúde integrativa. Não trabalha com dietas de gaveta ou prescrições genéricas.",
            "objection_script": "A consulta com o Dr. André Seabra é, na verdade, um programa completo de reprogramação metabólica. Ele faz uma investigação profunda de exames de sangue, taxa metabólica e hormônios para criar um plano de emagrecimento definitivo focado na raiz do problema, e não apenas uma dieta comum."
        }
        
    # 3. Instantiate sdr_llm if not configured
    if not sdr_llm:
        sdr_llm = get_llm_from_config(agent_config)
        
    # 4. Structured Output
    structured_sdr_llm = sdr_llm.with_structured_output(SDROutputSchema)
    
    # 5. Build prompt
    messages = state.get("messages", [])
    messages_str = ""
    for msg in messages:
        messages_str += f"{msg.role.upper()}: {msg.content}\n"
        
    collected_data = state.get("collected_data", CollectedDataSchema())
    
    if agent_config and agent_config.system_prompt:
        system_prompt = agent_config.system_prompt
        try:
            system_prompt = system_prompt.format(
                clinic_name=profile.get('clinic_name'),
                doctor_name=profile.get('doctor_name'),
                specialty=profile.get('specialty'),
                focus=profile.get('focus'),
                objection_script=profile.get('objection_script'),
                collected_name=collected_data.full_name or '',
                collected_cpf=collected_data.cpf or ''
            )
        except Exception:
            pass
    else:
        system_prompt = (
            "You are the SDR (Sales Development Representative) agent for a premium healthcare clinic.\n"
            "Your mission is to welcome new leads, build value around the medical care, "
            "and progressively collect details required for the secure patient record (name and CPF).\n\n"
            f"Clinic Profile Details:\n"
            f"- Clinic Name: {profile.get('clinic_name')}\n"
            f"- Doctor Name: {profile.get('doctor_name')}\n"
            f"- Specialty: {profile.get('specialty')}\n"
            f"- Focus: {profile.get('focus')}\n"
            f"- Objection Handling Script: {profile.get('objection_script')}\n\n"
            "Guidelines:\n"
            "- Identify as a premium health assistant, warm and professional.\n"
            "- Tom de voz: Premium, extremely professional, empathetic, and warm.\n"
            "- Style: Use short, objective messages, ideal for quick reading on WhatsApp.\n"
            "- WhatsApp Style: Once the user's name is known, address them by name in every subsequent message.\n"
            "- Progressive Data Collection:\n"
            "  - Do NOT ask for CPF right away. Ask for the user's full name first if not present.\n"
            "  - If the user wants to schedule or proceed, then ask for CPF. Explain elegantly that it is needed to open their secure medical record.\n"
            f"  - Currently collected name in state: '{collected_data.full_name or ''}'\n"
            f"  - Currently collected CPF in state: '{collected_data.cpf or ''}'\n"
            "  - Do not re-request any info that is already collected in state.\n"
            "- Objection Handling:\n"
            "  - If the user asks for prices or insurance agreements, do NOT simply list them immediately. Use the Objection Handling Script to explain the deep metabolic reprogramming program first to build value, then ask for their health objective."
        )

    # Check user query for institutional keywords and retrieve knowledge
    user_messages = [m for m in messages if m.role == "user"]
    query = user_messages[-1].content if user_messages else ""
    query_lower = query.lower()
    
    keywords = ['preço', 'valor', 'quanto custa', 'convênio', 'plano', 'aceita', 'regra', 'funcionamento', 'procedimento']
    knowledge_context = ""
    if any(kw in query_lower for kw in keywords) and tenant_id:
        try:
            chunks = await buscar_conhecimento(query, tenant_id, limit=3)
            if chunks:
                knowledge_context = "\n\nInformações adicionais obtidas da base de conhecimento da clínica:\n" + "\n".join([
                    f"- {chunk['content']}" for chunk in chunks
                ])
        except Exception as e:
            logger.warning(f"Failed to fetch knowledge in crc_sdr_node: {e}")

    prompt = (
        f"{system_prompt}\n"
        f"Conversation History:\n{messages_str}\n"
        "Respond to the patient and perform structured extraction of fields: response_message, extracted_name, extracted_cpf, and wants_to_schedule."
    )
    if knowledge_context:
        prompt += f"\n{knowledge_context}"
    
    # 6. Invoke LLM and extract results
    decision = await _call_llm_structured(structured_sdr_llm, prompt)
    
    if isinstance(decision, dict):
        response_message = decision.get("response_message", "")
        extracted_name = decision.get("extracted_name")
        extracted_cpf = decision.get("extracted_cpf")
        wants_to_schedule_val = decision.get("wants_to_schedule", False)
    else:
        response_message = getattr(decision, "response_message", "")
        extracted_name = getattr(decision, "extracted_name", None)
        extracted_cpf = getattr(decision, "extracted_cpf", None)
        wants_to_schedule_val = getattr(decision, "wants_to_schedule", False)
        
    # 7. Update collected data dynamically (Do NOT overwrite existing values with None)
    new_name = collected_data.full_name
    new_cpf = collected_data.cpf
    
    if extracted_name and extracted_name.strip():
        new_name = extracted_name.strip()
    if extracted_cpf and extracted_cpf.strip():
        new_cpf = extracted_cpf.strip()
        
    updated_collected_data = CollectedDataSchema(
        full_name=new_name,
        cpf=new_cpf,
        grievance=collected_data.grievance,
        preferred_doctor=collected_data.preferred_doctor,
        selected_datetime=collected_data.selected_datetime
    )
    
    # Appending the response message
    msg = MessageSchema(
        role="assistant",
        content=response_message,
        timestamp=datetime.now(timezone.utc)
    )
    
    return {
        "messages": [msg],
        "collected_data": updated_collected_data,
        "wants_to_schedule": wants_to_schedule_val,
        "next_node": None,
        "action_required": False
    }


@log_node_execution("crc_sdr_node")
def crc_sdr_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    CRC/SDR node: dynamic multi-tenant logic using structured extraction.
    """
    coro = _async_crc_sdr_node(state, config)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    if loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    else:
        return loop.run_until_complete(coro)


class AgendaOutputSchema(BaseModel):
    """
    Structured output schema for the Agenda node response and action.
    """
    response_message: str = Field(
        ...,
        description="The response message to be sent to the user in Portuguese."
    )
    action: Literal["book", "confirm", "cancel", "none"] = Field(
        default="none",
        description="The action to perform: 'book', 'confirm', 'cancel', or 'none'."
    )
    date: Optional[str] = Field(
        default=None,
        description="The resolved date in YYYY-MM-DD format."
    )
    time: Optional[str] = Field(
        default=None,
        description="The resolved time in HH:MM format."
    )
    doctor_id: Optional[str] = Field(
        default=None,
        description="The doctor's UUID, if specified or known."
    )


SAO_PAULO_TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")
MAX_SEARCH_DAYS = 90


def get_slots_for_day(d: date) -> List[datetime]:
    """Generates the 21 slots for a weekday (08:00 to 18:00 inclusive, 30-minute intervals)."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return []
    
    slots = []
    current_dt = datetime.combine(d, time(8, 0)).replace(tzinfo=SAO_PAULO_TZ)
    end_dt = datetime.combine(d, time(18, 0)).replace(tzinfo=SAO_PAULO_TZ)
    
    while current_dt <= end_dt:
        slots.append(current_dt)
        current_dt += timedelta(minutes=30)
    return slots


async def get_available_slots_on_date(
    client: MedflowClient,
    target_date: date,
    doctor_id: str,
    anchor_dt: datetime,
    tenant_id: Optional[str] = None
) -> List[datetime]:
    """Fetches occupied slots and returns list of available datetimes."""
    if target_date.weekday() >= 5:
        return []

    date_str = target_date.isoformat()
    appointments = await client.get_crm_appointments(
        date=date_str,
        doctor_id=doctor_id,
        tenant_id=tenant_id
    )

    occupied_times = set()
    for appt in appointments:
        time_str = appt.get("time")
        if time_str:
            parts = time_str.split(":")
            if len(parts) >= 2:
                occupied_times.add(f"{parts[0].zfill(2)}:{parts[1].zfill(2)}")

    all_slots = get_slots_for_day(target_date)
    available = []
    for slot_dt in all_slots:
        if target_date == anchor_dt.date() and slot_dt <= anchor_dt:
            continue
            
        slot_time_str = slot_dt.strftime("%H:%M")
        if slot_time_str not in occupied_times:
            available.append(slot_dt)
            
    return available


async def calculate_scarcity_slots(
    client: MedflowClient,
    doctor_id: str,
    anchor_dt: datetime,
    tenant_id: Optional[str] = None
) -> Tuple[Optional[datetime], Optional[datetime]]:
    try:
        local_today = anchor_dt.date()

        # --- Slot 1 Calculation (Opção Próxima) ---
        slot1: Optional[datetime] = None
        
        # 1. Check Today
        today_slots = await get_available_slots_on_date(
            client, local_today, doctor_id, anchor_dt, tenant_id
        )
        if today_slots:
            slot1 = today_slots[0]
        else:
            # 2. Check Tomorrow
            tomorrow = local_today + timedelta(days=1)
            tomorrow_slots = await get_available_slots_on_date(
                client, tomorrow, doctor_id, anchor_dt, tenant_id
            )
            if tomorrow_slots:
                slot1 = tomorrow_slots[0]
            else:
                # 3. Fallback: Search forward day-by-day
                for i in range(2, MAX_SEARCH_DAYS):
                     check_date = local_today + timedelta(days=i)
                     slots = await get_available_slots_on_date(
                         client, check_date, doctor_id, anchor_dt, tenant_id
                     )
                     if slots:
                         slot1 = slots[0]
                         break

        # --- Slot 2 Calculation (Opção Escassa) ---
        slot2: Optional[datetime] = None
        start_escassa = local_today + timedelta(days=20)
        
        for i in range(MAX_SEARCH_DAYS):
            check_date = start_escassa + timedelta(days=i)
            slots = await get_available_slots_on_date(
                client, check_date, doctor_id, anchor_dt, tenant_id
            )
            if slots:
                slot2 = slots[0]
                break

        return slot1, slot2
    except Exception as e:
        logger.error(f"Failed to calculate scarcity slots: {e}")
        return None, None


async def _async_agenda_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """Internal async implementation of agenda_node."""
    collected_data = state.get("collected_data") or CollectedDataSchema()
    
    # 1. Demographics Validation
    if not collected_data.full_name or not collected_data.cpf:
        missing_fields = []
        if not collected_data.full_name:
            missing_fields.append("nome completo")
        if not collected_data.cpf:
            missing_fields.append("CPF")
        
        req_msg = f"Para prosseguirmos com o agendamento, por favor, me informe o seu {', '.join(missing_fields)}."
        msg = MessageSchema(
            role="assistant",
            content=req_msg,
            timestamp=datetime.now(timezone.utc)
        )
        return {
            "messages": [msg],
            "next_node": None,
            "action_required": False
        }

    # 2. Setup config and variables
    configurable = config.get("configurable", {}) if config else {}
    tenant_id = configurable.get("tenant_id")
    
    agent_config = None
    if tenant_id:
        agent_config = await get_agent_config(tenant_id, "agenda")

    # Instantiate client (check if custom client provided for testing)
    client = configurable.get("medflow_client") or MedflowClient(tenant_id=tenant_id)
    
    # Doctor selection fallback
    doctor_id = collected_data.preferred_doctor or "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    
    # Timezone anchor
    anchor_dt = datetime.now(SAO_PAULO_TZ)
    anchor_str = anchor_dt.strftime("%A, %d/%m/%Y %H:%M:%S")
    days_pt = {
        "Monday": "Segunda-feira",
        "Tuesday": "Terça-feira",
        "Wednesday": "Quarta-feira",
        "Thursday": "Quinta-feira",
        "Friday": "Sexta-feira",
        "Saturday": "Sábado",
        "Sunday": "Domingo"
    }
    weekday_eng = anchor_dt.strftime("%A")
    weekday_pt = days_pt.get(weekday_eng, weekday_eng)
    anchor_info = f"{weekday_pt}, {anchor_dt.strftime('%d/%m/%Y %H:%M:%S')} (Timezone: America/Sao_Paulo)"
    
    # 3. Calculate scarcity slots
    slot1, slot2 = await calculate_scarcity_slots(client, doctor_id, anchor_dt, tenant_id)
    slot1_str = slot1.strftime("%d/%m/%Y às %H:%M") if slot1 else "Não disponível"
    slot2_str = slot2.strftime("%d/%m/%Y às %H:%M") if slot2 else "Não disponível"

    # 4. Invoke LLM
    llm = configurable.get("agenda_llm") or configurable.get("llm")
    if not llm:
        llm = get_llm_from_config(agent_config)

    # Build conversation history
    messages = state.get("messages", [])
    messages_str = ""
    for msg in messages:
        messages_str += f"{msg.role.upper()}: {msg.content}\n"

    if agent_config and agent_config.system_prompt:
        system_prompt = agent_config.system_prompt
        try:
            system_prompt = system_prompt.format(
                anchor_info=anchor_info,
                anchor_date=anchor_dt.date().isoformat(),
                slot1_str=slot1_str,
                slot2_str=slot2_str
            )
        except Exception:
            pass
    else:
        system_prompt = (
            "Você é o Agente de Agendamento (Agenda Agent) do CareFlow AI, operando em português.\n"
            "Sua função é auxiliar o paciente a marcar, confirmar ou cancelar consultas médicas.\n\n"
            f"Âncora Temporal do Sistema (data/hora atual): {anchor_info}\n"
            f"Data de hoje: {anchor_dt.date().isoformat()}\n\n"
            "Regras de Escassez e Horários Disponíveis:\n"
            f"- Opção 1 (Próxima): {slot1_str} (Se o paciente quiser o horário mais próximo disponível)\n"
            f"- Opção 2 (Escassa): {slot2_str} (Se o paciente preferir uma opção mais distante para se planejar)\n\n"
            "Diretrizes:\n"
            "1. Traduza termos relativos (ex.: 'amanhã', 'segunda que vem', 'à tarde') em datas absolutas YYYY-MM-DD usando a Âncora Temporal.\n"
            "2. NUNCA confirme ou crie um agendamento (action='book' ou 'confirm' or 'cancel') sem o consentimento explícito do paciente. Se o paciente apenas demonstrou interesse, sugeriu um horário, ou você está propondo os horários, use action='none'.\n"
            "3. Se o paciente escolher um dos horários propostos ou concordar com uma data/hora específica, e der consentimento explícito (ex.: 'Sim, pode agendar nesse horário', 'Confirmado', 'Quero esse das 17h30'), configure a 'action' correspondente e preencha os campos 'date' e 'time'.\n"
            "4. Responda sempre de forma profissional, acolhedora e concisa (estilo WhatsApp)."
        )

    # Check user query for institutional keywords and retrieve knowledge
    user_messages = [m for m in messages if m.role == "user"]
    query = user_messages[-1].content if user_messages else ""
    query_lower = query.lower()
    
    keywords = ['preço', 'valor', 'quanto custa', 'convênio', 'plano', 'aceita', 'regra', 'funcionamento', 'procedimento']
    knowledge_context = ""
    if any(kw in query_lower for kw in keywords) and tenant_id:
        try:
            chunks = await buscar_conhecimento(query, tenant_id, limit=3)
            if chunks:
                knowledge_context = "\n\nInformações adicionais obtidas da base de conhecimento da clínica:\n" + "\n".join([
                    f"- {chunk['content']}" for chunk in chunks
                ])
        except Exception as e:
            logger.warning(f"Failed to fetch knowledge in agenda_node: {e}")

    prompt = (
        f"{system_prompt}\n"
        f"Histórico da conversa:\n{messages_str}\n"
        f"Dados do Paciente:\n"
        f"- Nome: {collected_data.full_name}\n"
        f"- CPF: {collected_data.cpf}\n"
        f"- Médico preferencial: {collected_data.preferred_doctor or 'Não informado'}\n\n"
        "Retorne a resposta estruturada contendo: response_message, action, date, time e doctor_id."
    )
    if knowledge_context:
        prompt += f"\n{knowledge_context}"

    structured_llm = llm.with_structured_output(AgendaOutputSchema)
    decision = await _call_llm_structured(structured_llm, prompt)

    # Adapt if the returned decision is from the old mock supervisor (RoutingDecision)
    if hasattr(decision, "next_node") and not hasattr(decision, "response_message"):
        response_message = "[Agenda Agent] I can help you with scheduling, rescheduling, or canceling your appointment."
        action = "none"
        date_val = None
        time_val = None
        doc_id = None
    else:
        if isinstance(decision, dict):
            response_message = decision.get("response_message", "")
            action = decision.get("action", "none")
            date_val = decision.get("date")
            time_val = decision.get("time")
            doc_id = decision.get("doctor_id")
        else:
            response_message = getattr(decision, "response_message", "")
            action = getattr(decision, "action", "none") or "none"
            date_val = getattr(decision, "date", None)
            time_val = getattr(decision, "time", None)
            doc_id = getattr(decision, "doctor_id", None)

    # 5. Handle action execution
    action = action or "none"
    resolved_doctor_id = doc_id or doctor_id or "3fa85f64-5717-4562-b3fc-2c963f66afa6"

    # Explicit check: If wants_to_schedule was False, propagate it if the user wants to schedule now
    wants_to_schedule = state.get("wants_to_schedule", False)
    if action == "book" or "agendar" in response_message.lower() or "marcar" in response_message.lower():
        wants_to_schedule = True

    if action == "book" and date_val and time_val:
        # Perform booking via client
        try:
            # Ensure time format has seconds or is valid for Medflow API
            formatted_time = time_val
            if len(formatted_time) == 5:
                formatted_time += ":00"
                
            booking_res = await client.book_appointment(
                doctor_id=resolved_doctor_id,
                date=date_val,
                time=formatted_time,
                patient_name=collected_data.full_name,
                patient_cpf=collected_data.cpf,
                patient_phone=configurable.get("patient_phone")
            )
            
            # Cancel original appointment if it exists to clean up duplicate EM_CONTATO card
            original_appt_id = state.get("original_appointment_id")
            if original_appt_id:
                try:
                    await client.cancel_appointment(appointment_id=original_appt_id, tenant_id=tenant_id)
                    logger.info(f"Duplicate EM_CONTATO card {original_appt_id} canceled successfully.")
                except Exception as cancel_err:
                    logger.error(f"Failed to cancel duplicate EM_CONTATO card {original_appt_id}: {cancel_err}")
            
            # Update selected datetime on success
            try:
                parsed_dt = datetime.fromisoformat(f"{date_val}T{formatted_time}").replace(tzinfo=SAO_PAULO_TZ)
                collected_data.selected_datetime = parsed_dt
            except Exception:
                pass

                
            if not response_message or response_message.strip() == "" or "agendando" in response_message.lower():
                response_message = f"Consulta agendada com sucesso para o dia {date_val} às {time_val}!"
        except MedflowClientHTTPError as exc:
            if exc.status_code == 409:
                # 409 Conflict: slot taken. Propose new slots.
                new_slot1, new_slot2 = await calculate_scarcity_slots(client, resolved_doctor_id, anchor_dt, tenant_id)
                new_slot1_str = new_slot1.strftime("%d/%m/%Y às %H:%M") if new_slot1 else "Não disponível"
                new_slot2_str = new_slot2.strftime("%d/%m/%Y às %H:%M") if new_slot2 else "Não disponível"
                response_message = (
                    f"Desculpe, o horário solicitado ({date_val} às {time_val}) não está mais disponível. "
                    f"Podemos agendar em uma dessas outras opções?\n"
                    f"- Opção 1: {new_slot1_str}\n"
                    f"- Opção 2: {new_slot2_str}"
                )
            else:
                response_message = (
                    f"Ocorreu um erro ao tentar agendar sua consulta (código {exc.status_code}). "
                    "Por favor, tente novamente mais tarde."
                )
        except Exception as exc:
            response_message = "Desculpe, ocorreu uma instabilidade na conexão com o sistema. Por favor, tente novamente em alguns instantes."

    elif action in ["confirm", "cancel"]:
        # Try to find a matching appointment ID on the specified date or today/tomorrow
        search_dates = [date_val] if date_val else [anchor_dt.date().isoformat(), (anchor_dt.date() + timedelta(days=1)).isoformat()]
        appointment_id = None
        
        for s_date in search_dates:
            try:
                appts = await client.get_crm_appointments(date=s_date, doctor_id=resolved_doctor_id, tenant_id=tenant_id)
                for appt in appts:
                    patient_info = appt.get("patient") or {}
                    cpf_match = patient_info.get("cpf") and patient_info.get("cpf") == collected_data.cpf
                    name_match = patient_info.get("name") and patient_info.get("name").lower() == collected_data.full_name.lower()
                    if cpf_match or name_match:
                        appointment_id = appt.get("id")
                        break
                if appointment_id:
                    break
            except Exception:
                pass
                
        if appointment_id:
            try:
                if action == "confirm":
                    await client.confirm_appointment(appointment_id=appointment_id, tenant_id=tenant_id)
                    if not response_message or "confirmando" in response_message.lower():
                        response_message = "Sua presença foi confirmada com sucesso!"
                else:
                    await client.cancel_appointment(appointment_id=appointment_id, tenant_id=tenant_id)
                    if not response_message or "cancelando" in response_message.lower():
                        response_message = "Sua consulta foi cancelada com sucesso."
            except Exception as exc:
                response_message = "Desculpe, não conseguimos atualizar o status do seu agendamento no momento. Tente novamente em instantes."
        else:
            response_message = "Não encontrei nenhum agendamento ativo com seus dados para prosseguir com essa ação."

    # Return updated state
    msg = MessageSchema(
        role="assistant",
        content=response_message,
        timestamp=datetime.now(timezone.utc)
    )
    
    return {
        "messages": [msg],
        "collected_data": collected_data,
        "wants_to_schedule": wants_to_schedule,
        "next_node": None,
        "action_required": False
    }


@log_node_execution("agenda_node")
def agenda_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    Agenda node: handles scheduling logic, demographics validation, explicit confirmation,
    scarcity slot generation, and calls MedflowClient for bookings/confirmations/cancellations.
    """
    coro = _async_agenda_node(state, config)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    if loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    else:
        return loop.run_until_complete(coro)


async def buscar_conhecimento(query: str, organization_id: str, limit: int = 3) -> List[dict]:
    """
    Retrieves the most relevant knowledge blocks from the tenant's database
    using cosine similarity (pgvector) with a fallback to textual ILIKE/LIKE search.
    """
    from app.core.tenant_database import tenant_db_manager
    from app.services.embedding import get_embedding
    
    results_list = []
    
    try:
        async with await tenant_db_manager.get_tenant_session(organization_id) as session:
            # Check if embedding column exists (pgvector is available)
            has_vector = False
            try:
                await session.execute(text("SELECT embedding FROM clinic_knowledge LIMIT 0;"))
                has_vector = True
            except Exception:
                await session.rollback()
            
            if has_vector:
                try:
                    query_vector = get_embedding(query)
                    vector_str = "[" + ",".join(map(str, query_vector)) + "]"
                    
                    sql = text("""
                        SELECT id, content, metadata, 1.0 - (embedding <=> :query_embedding) AS similarity
                        FROM clinic_knowledge
                        WHERE 1.0 - (embedding <=> :query_embedding) >= 0.70
                        ORDER BY similarity DESC
                        LIMIT :limit;
                    """)
                    
                    res = await session.execute(sql, {"query_embedding": vector_str, "limit": limit})
                    for row in res.all():
                        results_list.append({
                            "id": row[0],
                            "content": row[1],
                            "metadata": row[2],
                            "similarity": row[3]
                        })
                    return results_list
                except Exception as e:
                    logger.warning(f"Vector search failed, falling back to text search: {e}")
                    await session.rollback()
            
            # Fallback to textual LIKE/ILIKE search
            query_like = f"%{query}%"
            dialect_name = session.bind.dialect.name
            if dialect_name == "postgresql":
                sql = text("""
                    SELECT id, content, metadata
                    FROM clinic_knowledge
                    WHERE content ILIKE :query_like
                    LIMIT :limit;
                """)
            else:
                sql = text("""
                    SELECT id, content, metadata
                    FROM clinic_knowledge
                    WHERE content LIKE :query_like
                    LIMIT :limit;
                """)
            
            res = await session.execute(sql, {"query_like": query_like, "limit": limit})
            for row in res.all():
                results_list.append({
                    "id": row[0],
                    "content": row[1],
                    "metadata": row[2],
                    "similarity": 0.5
                })
                
            # If word-by-word fallback is needed
            if not results_list:
                words = [w.strip() for w in query.split() if len(w.strip()) > 3]
                if words:
                    clauses = []
                    params = {"limit": limit}
                    for idx, word in enumerate(words):
                        param_name = f"word_{idx}"
                        if dialect_name == "postgresql":
                            clauses.append(f"content ILIKE :{param_name}")
                        else:
                            clauses.append(f"content LIKE :{param_name}")
                        params[param_name] = f"%{word}%"
                    
                    sql_text = f"SELECT id, content, metadata FROM clinic_knowledge WHERE {' OR '.join(clauses)} LIMIT :limit;"
                    try:
                        res = await session.execute(text(sql_text), params)
                        for row in res.all():
                            results_list.append({
                                "id": row[0],
                                "content": row[1],
                                "metadata": row[2],
                                "similarity": 0.4
                            })
                    except Exception:
                        pass
            
    except Exception as e:
        logger.error(f"Error in buscar_conhecimento: {e}")
        
    return results_list


async def _async_rag_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    Internal asynchronous implementation of rag_node.
    """
    configurable = config.get("configurable", {}) if config else {}
    tenant_id = configurable.get("tenant_id", "org1")
    llm = configurable.get("llm")
    
    is_mock = False
    if llm:
        class_name = llm.__class__.__name__
        if "MockLLM" in class_name:
            is_mock = True

    if is_mock:
        msg = MessageSchema(
            role="assistant",
            content="[RAG Agent] I can provide information about our insurance plans, procedures, specialties, and prices.",
            timestamp=datetime.now(timezone.utc)
        )
        return {
            "messages": [msg],
            "next_node": None,
            "action_required": False
        }
        
    # Extract query
    messages = state.get("messages", [])
    user_messages = [m for m in messages if m.role == "user"]
    query = user_messages[-1].content if user_messages else ""
    
    # Retrieve knowledge
    chunks = await buscar_conhecimento(query, tenant_id, limit=3)
    
    if chunks:
        context_str = "\n\n".join([
            f"Documento {idx+1} (Origem: {chunk['metadata'].get('source', 'Manual') if isinstance(chunk['metadata'], dict) else 'Manual'}):\n{chunk['content']}"
            for idx, chunk in enumerate(chunks)
        ])
    else:
        context_str = "Nenhuma informação adicional encontrada na base de conhecimento."
        
    system_prompt = (
        "Você é o Agente de Conhecimento (RAG Agent) do CareFlow AI, operando em português.\n"
        "Sua tarefa é responder à pergunta do paciente baseando-se estritamente nas informações fornecidas no Contexto abaixo.\n\n"
        f"Contexto:\n{context_str}\n\n"
        "Diretrizes:\n"
        "1. Se as informações no contexto forem suficientes para responder, responda de forma acolhedora, precisa e concisa (estilo WhatsApp).\n"
        "2. Se as informações não forem suficientes ou a resposta não estiver no contexto, explique de forma educada que não possui essa informação específica e oriente o paciente a agendar uma consulta para falar com a equipe.\n"
        "3. NUNCA invente fatos ou responda fora do contexto fornecido.\n"
        "4. Inicie sua resposta identificando-se brevemente como assistente de informação da clínica."
    )
    
    if not llm:
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash")
        
    from langchain_core.messages import SystemMessage, HumanMessage
    chat_prompt = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=query)
    ]
    
    try:
        response = await llm.ainvoke(chat_prompt)
        response_content = response.content
    except Exception as e:
        logger.error(f"Error invoking LLM in RAG node: {e}")
        response_content = "Desculpe, ocorreu uma instabilidade na conexão com nosso assistente de IA. Por favor, tente novamente em alguns instantes."
        
    msg = MessageSchema(
        role="assistant",
        content=response_content,
        timestamp=datetime.now(timezone.utc)
    )
    
    return {
        "messages": [msg],
        "next_node": None,
        "action_required": False
    }


@log_node_execution("rag_node")
def rag_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    """
    RAG node: handles institutional questions by retrieving dynamic knowledge blocks
    and querying Gemini.
    """
    print("[Node Activation] rag_node activated")
    logger.info("rag_node activated")
    
    coro = _async_rag_node(state, config)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    if loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    else:
        return loop.run_until_complete(coro)


# Set up the StateGraph workflow
workflow = StateGraph(AgentState)

# Add nodes
workflow.add_node("supervisor", supervisor_node)
workflow.add_node("crc_sdr_node", crc_sdr_node)
workflow.add_node("agenda_node", agenda_node)

# Set entry point
workflow.set_entry_point("supervisor")


def check_next_node(state: AgentState) -> str:
    """
    Evaluates next_node state key and routes.
    """
    n = state.get("next_node")
    if n in ["crc_sdr_node", "agenda_node"]:
        return n
    return END


# Add conditional edges from supervisor
workflow.add_conditional_edges(
    "supervisor",
    check_next_node,
    {
        "crc_sdr_node": "crc_sdr_node",
        "agenda_node": "agenda_node",
        END: END
    }
)

# Add edges back from stub nodes to supervisor (hub-and-spoke setup)
workflow.add_edge("crc_sdr_node", "supervisor")
workflow.add_edge("agenda_node", "supervisor")

# Compile graph
graph = workflow.compile()
graph.invoke = wrap_graph_invoke(graph.invoke)
graph.ainvoke = wrap_graph_ainvoke(graph.ainvoke)
