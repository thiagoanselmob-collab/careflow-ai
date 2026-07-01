import logging

logger = logging.getLogger(__name__)

class WhatsAppClient:
    """
    Service stub for sending messages to users via the WhatsApp Cloud API.
    """
    async def send_message(self, phone_number: str, text: str, organization_id: str) -> bool:
        """
        Sends a WhatsApp text message to the specified phone number.
        In this stub implementation, it simply logs the event and returns True.
        """
        logger.info(f"[WhatsApp STUB] Sending message to {phone_number} for organization {organization_id}: {text}")
        return True

whatsapp_client = WhatsAppClient()
