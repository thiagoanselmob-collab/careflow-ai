# Phase 3.3 Backend Implementation Design Report

This report provides the detailed design and implementation specifications for the Medflow CRM client integration and the agenda scarcity scheduling rules in the CareFlow AI backend.

---

## 1. Observations

### 1.1. Dependency Analysis (`pyproject.toml`)
We examined the file `/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend/pyproject.toml` and observed the following dependencies:
- **FastAPI / Pydantic:** `fastapi = "^0.111.0"`, `pydantic = "^2.7.4"`, `pydantic-settings = "^2.3.3"`.
- **HTTP Client:** `httpx = ">=0.28.1"` is already listed in `[tool.poetry.dependencies]`, making it available for production services.
- **LangGraph & LangChain:** `langgraph = ">=0.1"`, `langchain = "^1.3.11"`, `langchain-google-genai = "^4.2.6"`, `langchain-anthropic = "^1.4.8"`.
- **Dev Dependencies:** `pytest = "^8.2.2"` and `pytest-asyncio = "^0.23.7"` are present in the `[tool.poetry.group.dev.dependencies]` section.
- **Mocking:** There is no third-party mocking library listed (such as `pytest-mock`), but Python's standard `unittest.mock` can be utilized.

### 1.2. Configuration Analysis (`app/core/config.py`)
- We observed that `Settings` loads environment configuration using Pydantic Settings.
- Currently, there are no environment parameters defined for the Medflow CRM base URL, integration token, or tenant defaults.
- Setting validation enforces that `database_url` and `redis_url` must not be default values in production.

### 1.3. LangGraph & Schemas Analysis (`app/services/agents/graph.py` and `app/schemas/session.py`)
- The state graph runs on `AgentState`, which inherits from `TypedDict` and holds:
  - `messages: List[MessageSchema]` (reduced via appending)
  - `bot_active: bool`
  - `collected_data: CollectedDataSchema`
  - `wants_to_schedule: Optional[bool]`
  - `next_node: Optional[str]`
  - `action_required: Optional[bool]`
- `CollectedDataSchema` (in `app/schemas/session.py`) holds fields:
  - `full_name: Optional[str]`
  - `cpf: Optional[str]`
  - `grievance: Optional[str]`
  - `preferred_doctor: Optional[str]`
  - `selected_datetime: Optional[datetime]`
- The `agenda_node` (lines 313–329 of `graph.py`) is currently a stub node that returns a mock message:
  ```python
  def agenda_node(state: AgentState) -> dict:
      """
      Agenda stub node: prints activation and appends a mock assistant message.
      """
      print("[Node Activation] agenda_node activated")
      logger.info("agenda_node activated")
      msg = MessageSchema(
          role="assistant",
          content="[Agenda Agent] I can help you with scheduling, rescheduling, or canceling your appointment.",
          timestamp=datetime.now(timezone.utc)
      )
      return {
          "messages": [msg],
          "next_node": None,
          "action_required": False
      }
  ```

### 1.4. API Contracts (`docs/medflow_api_contracts.md`)
We observed the following CRM API specifications:
- **Authentication:** Bearer token via `Authorization: Bearer <JWT_TOKEN>`.
- **Multi-Tenancy:** Handled via the custom header `X-Tenant-ID: <ORGANIZATION_UUID>` for OWNER operations or resolved from user profile context.
- **Idempotency Key:** Modified endpoints accept `Idempotency-Key: <UNIQUE_KEY>` to guarantee no duplicate state mutation.
- **Endpoints:**
  1. `GET /api/appointments/crm?date=YYYY-MM-DD&doctorId=<id>`: Retrieves active appointments.
  2. `POST /api/webhooks/n8n/book-appointment`: Registers an appointment.
  3. `POST /api/webhooks/n8n/confirm-appointment/{appointmentId}`: Confirms an appointment.
  4. `POST /api/webhooks/n8n/cancel-appointment/{appointmentId}`: Cancels an appointment.
  5. `PATCH /api/appointments/{id}/status`: Updates appointment status.

---

## 2. Logic Chain

1. **Dependency Availability:** Since `httpx` is in `pyproject.toml`, we can import it in `app/services/medflow_client.py` without introducing new dependencies.
2. **Client Config Integration:** To configure `MedflowClient` cleanly, we should add settings fields to `Settings` in `app/core/config.py`:
   - `medflow_api_url: str = Field(default="https://api.medflowcrm.com", validation_alias="MEDFLOW_API_URL")`
   - `medflow_api_token: str = Field(default="dev-token", validation_alias="MEDFLOW_API_TOKEN")`
3. **Idempotency Generation:** To ensure robustness for write mutations (`POST`, `PATCH`), the client must generate a UUID v4 key automatically if none is supplied.
4. **Timezone Accuracy:** The appointments list is fetched on a daily basis. The current datetime must be localized to `America/Sao_Paulo` to extract the correct today's date and past-slot checks. Comparing UTC dates directly would cause queries to target the wrong local day during evening hours (due to timezone offsets).
5. **Business Hours Resolution:** 21 slots are generated between 08:00 and 18:00 (inclusive). Weekends are skipped. Today's slots are filtered to keep only future slots.
6. **Scarcity Strategy (Slot 1 & 2):**
   - Slot 1 looks at today and tomorrow. If both are empty, it enters a fallback loop searching forward day-by-day.
   - Slot 2 starts 20 days from now and searches forward day-by-day.
   - To prevent potential infinite loops during fallback (e.g. if a doctor is entirely unavailable), we introduce a hard limit of `MAX_SEARCH_DAYS = 90`.

---

## 3. Implementation Plan: `MedflowClient`

The client will be created as `app/services/medflow_client.py` and implement standard HTTP wrappers around the CRM REST endpoints.

```python
import uuid
import logging
import httpx
from typing import List, Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

class MedflowClientError(Exception):
    """Exception raised when a Medflow CRM API request fails."""
    def __init__(self, status_code: int, message: str, details: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.message = message
        self.details = details
        super().__init__(f"Medflow API error {status_code}: {message}")

class MedflowClient:
    """
    Asynchronous client for interacting with the Medflow CRM API endpoints.
    Manages HTTP request construction, multi-tenancy, authentication,
    and automatic idempotency key injection.
    """
    def __init__(
        self,
        base_url: str = settings.medflow_api_url,
        default_token: str = settings.medflow_api_token,
        client: Optional[httpx.AsyncClient] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.default_token = default_token
        self._client = client or httpx.AsyncClient()

    async def close(self) -> None:
        """Closes the underlying HTTPX AsyncClient session."""
        await self._client.aclose()

    def _get_headers(
        self,
        token: Optional[str] = None,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        is_mutation: bool = False
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Auth Token
        auth_token = token or self.default_token
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            
        # Multi-Tenancy Isolation
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id

        # Idempotency injection for state mutation requests
        if is_mutation:
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())

        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = await self._client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers
            )
            
            if response.is_success:
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()
            
            # Error Handling
            try:
                error_body = response.json()
                message = error_body.get("message", "API response error")
                details = error_body.get("details")
            except (ValueError, KeyError):
                message = response.text or "API response error"
                details = None
                
            raise MedflowClientError(
                status_code=response.status_code,
                message=message,
                details=details
            )
            
        except httpx.RequestError as e:
            logger.error(f"HTTP request failed: {e}")
            raise MedflowClientError(
                status_code=500,
                message=f"Network communication failed: {str(e)}"
            )

    async def get_crm_appointments(
        self,
        date: str,
        doctor_id: str = "all",
        organization_id: Optional[str] = None,
        token: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """GET /api/appointments/crm"""
        path = "/api/appointments/crm"
        params = {"date": date, "doctorId": doctor_id}
        if organization_id:
            params["organizationId"] = organization_id
        
        headers = self._get_headers(token=token, tenant_id=organization_id, is_mutation=False)
        return await self._request("GET", path, params=params, headers=headers)

    async def book_appointment(
        self,
        doctor_id: str,
        date: str,
        time: str,
        patient_name: str,
        patient_phone: Optional[str] = None,
        patient_cpf: Optional[str] = None,
        patient_email: Optional[str] = None,
        procedure: Optional[str] = None,
        notes: Optional[str] = None,
        organization_id: Optional[str] = None,
        token: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/webhooks/n8n/book-appointment"""
        path = "/api/webhooks/n8n/book-appointment"
        payload = {
            "doctorId": doctor_id,
            "date": date,
            "time": time,
            "patientName": patient_name,
            "patientPhone": patient_phone,
            "patientCpf": patient_cpf,
            "patientEmail": patient_email,
            "procedure": procedure,
            "notes": notes
        }
        # Clean null values from payload
        payload = {k: v for k, v in payload.items() if v is not None}
        
        headers = self._get_headers(
            token=token,
            tenant_id=organization_id,
            idempotency_key=idempotency_key,
            is_mutation=True
        )
        return await self._request("POST", path, json_data=payload, headers=headers)

    async def confirm_appointment(
        self,
        appointment_id: str,
        organization_id: Optional[str] = None,
        token: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/webhooks/n8n/confirm-appointment/{appointmentId}"""
        path = f"/api/webhooks/n8n/confirm-appointment/{appointment_id}"
        headers = self._get_headers(
            token=token,
            tenant_id=organization_id,
            idempotency_key=idempotency_key,
            is_mutation=True
        )
        return await self._request("POST", path, headers=headers)

    async def cancel_appointment(
        self,
        appointment_id: str,
        organization_id: Optional[str] = None,
        token: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST /api/webhooks/n8n/cancel-appointment/{appointmentId}"""
        path = f"/api/webhooks/n8n/cancel-appointment/{appointment_id}"
        headers = self._get_headers(
            token=token,
            tenant_id=organization_id,
            idempotency_key=idempotency_key,
            is_mutation=True
        )
        return await self._request("POST", path, headers=headers)

    async def update_appointment_status(
        self,
        appointment_id: str,
        status: str,
        source: Optional[str] = None,
        organization_id: Optional[str] = None,
        token: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """PATCH /api/appointments/{id}/status"""
        path = f"/api/appointments/{appointment_id}/status"
        payload = {"status": status}
        if source:
            payload["source"] = source
            
        headers = self._get_headers(
            token=token,
            tenant_id=organization_id,
            idempotency_key=idempotency_key,
            is_mutation=True
        )
        return await self._request("PATCH", path, json_data=payload, headers=headers)
```

---

## 4. Scarcity Algorithm in `agenda_node`

This detailed algorithm computes Slot 1 (Opção Próxima) and Slot 2 (Opção Escassa) under the rules described in the prompt.

### 4.1. Rules Verification & Time Anchoring
- **Timezone:** `America/Sao_Paulo`.
- **Business Hours:** Monday to Friday, `08:00` to `18:00` (inclusive).
- **Interval:** 30 minutes.
- **Daily Slots:** 21 slots per day (`08:00`, `08:30`, ..., `18:00`).
- **Safety Margin:** Search cap `MAX_SEARCH_DAYS = 90` to avoid infinite loops.

### 4.2. Algorithm Outline

```python
import zoneinfo
from datetime import datetime, date, time, timedelta
from typing import List, Dict, Tuple, Optional

SAO_PAULO_TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")
MAX_SEARCH_DAYS = 90

def get_slots_for_day(d: date) -> List[datetime]:
    """Generates the 21 slots for a weekday."""
    if d.weekday() >= 5:  # Weekend
        return []
    
    slots = []
    current_dt = datetime.combine(d, time(8, 0)).replace(tzinfo=SAO_PAULO_TZ)
    end_dt = datetime.combine(d, time(18, 0)).replace(tzinfo=SAO_PAULO_TZ)
    
    while current_dt <= end_dt:
        slots.append(current_dt)
        current_dt += timedelta(minutes=30)
    return slots

async def get_available_slots_on_date(
    medflow_client: MedflowClient,
    target_date: date,
    doctor_id: str,
    anchor_dt: datetime,
    organization_id: Optional[str] = None,
    token: Optional[str] = None
) -> List[datetime]:
    """Fetches occupied slots and returns list of available datetimes."""
    if target_date.weekday() >= 5:
        return []

    # 1. Fetch active appointments for the day
    date_str = target_date.isoformat()
    try:
        appointments = await medflow_client.get_crm_appointments(
            date=date_str,
            doctor_id=doctor_id,
            organization_id=organization_id,
            token=token
        )
    except Exception as e:
        logger.error(f"Failed to fetch CRM appointments for {date_str}: {e}")
        return []

    # 2. Extract and normalize occupied times ("HH:MM")
    occupied_times = set()
    for appt in appointments:
        time_str = appt.get("time")
        if time_str:
            parts = time_str.split(":")
            if len(parts) >= 2:
                # Normalizes to 'HH:MM'
                occupied_times.add(f"{parts[0].zfill(2)}:{parts[1].zfill(2)}")

    # 3. Generate candidate slots and filter
    all_slots = get_slots_for_day(target_date)
    available = []
    for slot_dt in all_slots:
        # If it's today, it must be in the future relative to the anchor
        if target_date == anchor_dt.date() and slot_dt <= anchor_dt:
            continue
            
        slot_time_str = slot_dt.strftime("%H:%M")
        if slot_time_str not in occupied_times:
            available.append(slot_dt)
            
    return available

async def calculate_scarcity_slots(
    medflow_client: MedflowClient,
    doctor_id: str,
    anchor_dt: Optional[datetime] = None,
    organization_id: Optional[str] = None,
    token: Optional[str] = None
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Computes:
      - Slot 1 (Opção Próxima): Next free slot starting from today/tomorrow (and forward if none).
      - Slot 2 (Opção Escassa): Next free slot starting >= 20 days in the future.
    """
    # Initialize the anchor in Sao Paulo local timezone
    if anchor_dt is None:
        anchor_dt = datetime.now(SAO_PAULO_TZ)
    elif anchor_dt.tzinfo is None:
        anchor_dt = anchor_dt.replace(tzinfo=SAO_PAULO_TZ)
    else:
        anchor_dt = anchor_dt.astimezone(SAO_PAULO_TZ)

    local_today = anchor_dt.date()

    # --- Slot 1 Calculation ---
    slot1: Optional[datetime] = None
    
    # 1. Check Today
    today_slots = await get_available_slots_on_date(
        medflow_client, local_today, doctor_id, anchor_dt, organization_id, token
    )
    if today_slots:
        slot1 = today_slots[0]
    else:
        # 2. Check Tomorrow
        tomorrow = local_today + timedelta(days=1)
        tomorrow_slots = await get_available_slots_on_date(
            medflow_client, tomorrow, doctor_id, anchor_dt, organization_id, token
        )
        if tomorrow_slots:
            slot1 = tomorrow_slots[0]
        else:
            # 3. Fallback: Search forward day-by-day
            for i in range(2, MAX_SEARCH_DAYS):
                check_date = local_today + timedelta(days=i)
                slots = await get_available_slots_on_date(
                    medflow_client, check_date, doctor_id, anchor_dt, organization_id, token
                )
                if slots:
                    slot1 = slots[0]
                    break

    # --- Slot 2 Calculation ---
    slot2: Optional[datetime] = None
    start_escassa = local_today + timedelta(days=20)
    
    for i in range(MAX_SEARCH_DAYS):
        check_date = start_escassa + timedelta(days=i)
        slots = await get_available_slots_on_date(
            medflow_client, check_date, doctor_id, anchor_dt, organization_id, token
        )
        if slots:
            slot2 = slots[0]
            break

    return slot1, slot2
```

### 4.3. Integration within `agenda_node`
When `agenda_node` is activated:
1. Examine if `collected_data.selected_datetime` is already present.
2. If absent (or the user requests options):
   - Determine `doctor_id` (either from `collected_data.preferred_doctor` or default clinic config).
   - Execute `calculate_scarcity_slots`.
   - Pass Slot 1 and Slot 2 as context parameters to the LLM (e.g. ChatAnthropic / ChatGoogleGenerativeAI) to formulate a friendly, premium scheduling proposal.
3. If the user chooses a slot (or confirms booking):
   - Invoke `medflow_client.book_appointment` using the patient's name, CPF, selected slot datetime, and preferred doctor.
   - Catch `MedflowClientError` (such as 409 Conflict if the slot is booked concurrently) and return a helpful error response (e.g., suggesting new slots).
   - Clear scheduling state and append the booking confirmation to the messages history.

---

## 5. Test Cases Specification (`tests/test_agent_agenda.py`)

A set of automated tests using `pytest` and `pytest-asyncio` will be added to ensure code coverage of the scheduling client and algorithms.

### 5.1. Client Test Cases
1. **Header Resolution:**
   - Verify standard request headers format (JSON Content-Type, Accept).
   - Verify `Authorization` Bearer header is added correctly when a token is supplied or defaults are loaded.
   - Verify `X-Tenant-ID` is correctly injected in headers.
2. **Automatic Idempotency Key Generation:**
   - Verify mutations (`book_appointment`, `cancel_appointment`, `confirm_appointment`, `update_appointment_status`) include a valid UUID v4 string header if no custom key is provided.
   - Verify a custom idempotency key is preserved in headers.
3. **HTTP Error Raising:**
   - Mock error responses (400, 401, 403, 409, 422, 500) using a mock HTTP router and assert that the client raises a parsed `MedflowClientError` containing the correct status code and API message.

### 5.2. Scarcity Algorithm Test Cases
4. **Timezone Anchoring & Past Slots:**
   - Anchor time at Monday `16:15` (Sao Paulo time). Verify slots today before `16:15` (e.g. `16:00`) are omitted, and only `16:30`, `17:00`, `17:30`, `18:00` are returned as candidates.
5. **Weekend Skipping:**
   - Verify that generating slots for Saturday and Sunday returns an empty list, and the algorithm skips weekends to Monday.
6. **Appointment Exclusion:**
   - Set up active appointments at `09:30` and `14:00` on a weekday. Ensure those times are excluded from available slots.
7. **Slot 1 (Opção Próxima) Priority:**
   - Verify Slot 1 returns today's first available slot when available.
   - Verify Slot 1 returns tomorrow's first available slot when today is fully booked or is a weekend.
   - Verify Slot 1 fallback searches day-by-day and returns the first available slot on subsequent weekdays.
8. **Slot 2 (Opção Escassa) Range constraint:**
   - Verify Slot 2 begins checking exactly 20 days in the future.
   - Verify that if day 20 is a Saturday, it skips to day 22 (Monday) and returns the first available slot.
9. **Cap constraint (Safety Limit):**
   - Mock a doctor with 100% occupied slots for the next 100 days. Verify the search breaks safely at `MAX_SEARCH_DAYS` (90 days) instead of hanging indefinitely, returning `None` for slots.

---

## 6. Caveats

- **No Multi-doctor Consolidation:** The current 2-slot rules are scoped to a single doctor ID. If a user does not specify a preferred doctor, we assume a default clinic doctor or must list all doctor options. We should plan a fallback doctor selection step in the graph.
- **Concurrent Bookings (Race Conditions):** Although we filter slots based on a recent `GET /api/appointments/crm`, a slot could be booked by another user before the current user confirms. The client handles this by catching 409 Conflict, but the `agenda_node` must support a retry/re-prompt loop to guide the user back to selecting a new slot.
- **Manual Idempotency Keys:** For the `agenda_node` to safely retry, it should tie the idempotency key to the specific session ID or execution turn to prevent double booking.

---

## 7. Conclusions

The dependencies (`httpx` and `pytest`) are fully available. We have a robust, modular design for the `MedflowClient` and `agenda_node` scheduling logic that respects the timezone requirement, handles multi-tenancy correctly, secures mutations via idempotency keys, and computes scarcity-based slots cleanly.

---

## 8. Verification Method

To verify the implementation once coded:
1. **Code Inspections:** Verify that `app/services/medflow_client.py` has no compilation or imports errors.
2. **Execute Tests:** Run the test suite:
   ```bash
   poetry run pytest tests/test_agent_agenda.py
   ```
3. **Log Tracing:** Enable `DEBUG` logging on `app.services.medflow_client` and trace request execution to confirm headers and query params match.
