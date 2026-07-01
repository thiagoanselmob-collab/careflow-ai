import logging
import uuid
from typing import List, Optional, Any, Dict
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class MedflowClientError(Exception):
    """Base exception for MedflowClient errors."""
    pass


class MedflowClientHTTPError(MedflowClientError):
    """Exception raised when Medflow API returns an HTTP error response."""
    def __init__(self, status_code: int, response_body: str, message: str):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"Medflow HTTP Error {status_code}: {message}")


class MedflowClientConnectionError(MedflowClientError):
    """Exception raised for connection-related issues."""
    pass


class MedflowClient:
    """
    Asynchronous client for interacting with the Medflow Java Backend API.
    """
    def __init__(
        self,
        base_url: Optional[str] = None,
        jwt_token: Optional[str] = None,
        tenant_id: Optional[str] = None,
        timeout: float = 10.0
    ):
        self.base_url = (base_url or settings.medflow_api_url).rstrip("/")
        self.jwt_token = jwt_token or settings.medflow_jwt_token
        self.tenant_id = tenant_id
        self.timeout = timeout

    def _get_headers(
        self,
        idempotency_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        is_mutation: bool = False
    ) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.jwt_token}",
            "Accept": "application/json",
        }
        
        resolved_tenant = tenant_id or self.tenant_id
        if resolved_tenant:
            headers["X-Tenant-ID"] = resolved_tenant
            
        if is_mutation:
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
            headers["Content-Type"] = "application/json"
            
        return headers

    async def get_crm_appointments(
        self,
        date: str,
        doctor_id: str,
        tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        GET /api/appointments/crm
        """
        url = f"{self.base_url}/api/appointments/crm"
        params = {"date": date, "doctorId": doctor_id}
        headers = self._get_headers(tenant_id=tenant_id)
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params, headers=headers)
                if response.is_error:
                    raise MedflowClientHTTPError(
                        status_code=response.status_code,
                        response_body=response.text,
                        message=response.text
                    )
                return response.json()
        except httpx.RequestError as exc:
            raise MedflowClientConnectionError(f"HTTP request failed: {exc}") from exc

    async def update_appointment_status(
        self,
        appointment_id: str,
        status: str,
        source: Optional[str] = "N8N",
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        PATCH /api/appointments/{id}/status
        """
        url = f"{self.base_url}/api/appointments/{appointment_id}/status"
        headers = self._get_headers(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            is_mutation=True
        )
        data = {"status": status.upper().strip(), "source": source}
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.patch(url, json=data, headers=headers)
                if response.is_error:
                    raise MedflowClientHTTPError(
                        status_code=response.status_code,
                        response_body=response.text,
                        message=response.text
                    )
                return response.json()
        except httpx.RequestError as exc:
            raise MedflowClientConnectionError(f"HTTP request failed: {exc}") from exc

    async def patch_appointment_status(
        self,
        appointment_id: str,
        status: str,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        PATCH /api/appointments/{id}/status wrapper
        """
        return await self.update_appointment_status(
            appointment_id=appointment_id,
            status=status,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key
        )


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
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        POST /api/webhooks/n8n/book-appointment
        """
        url = f"{self.base_url}/api/webhooks/n8n/book-appointment"
        headers = self._get_headers(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            is_mutation=True
        )
        data = {
            "doctorId": doctor_id,
            "date": date,
            "time": time,
            "patientName": patient_name
        }
        if patient_phone:
            data["patientPhone"] = patient_phone
        if patient_cpf:
            data["patientCpf"] = patient_cpf
        if patient_email:
            data["patientEmail"] = patient_email
        if procedure:
            data["procedure"] = procedure
        if notes:
            data["notes"] = notes
            
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=data, headers=headers)
                if response.is_error:
                    raise MedflowClientHTTPError(
                        status_code=response.status_code,
                        response_body=response.text,
                        message=response.text
                    )
                return response.json()
        except httpx.RequestError as exc:
            raise MedflowClientConnectionError(f"HTTP request failed: {exc}") from exc

    async def confirm_appointment(
        self,
        appointment_id: str,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        POST /api/webhooks/n8n/confirm-appointment/{appointmentId}
        """
        url = f"{self.base_url}/api/webhooks/n8n/confirm-appointment/{appointment_id}"
        headers = self._get_headers(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            is_mutation=True
        )
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers)
                if response.is_error:
                    raise MedflowClientHTTPError(
                        status_code=response.status_code,
                        response_body=response.text,
                        message=response.text
                    )
                return response.json()
        except httpx.RequestError as exc:
            raise MedflowClientConnectionError(f"HTTP request failed: {exc}") from exc

    async def cancel_appointment(
        self,
        appointment_id: str,
        tenant_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        POST /api/webhooks/n8n/cancel-appointment/{appointmentId}
        """
        url = f"{self.base_url}/api/webhooks/n8n/cancel-appointment/{appointment_id}"
        headers = self._get_headers(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            is_mutation=True
        )
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers)
                if response.is_error:
                    raise MedflowClientHTTPError(
                        status_code=response.status_code,
                        response_body=response.text,
                        message=response.text
                    )
                return response.json()
        except httpx.RequestError as exc:
            raise MedflowClientConnectionError(f"HTTP request failed: {exc}") from exc
