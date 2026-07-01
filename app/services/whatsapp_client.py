import logging

logger = logging.getLogger(__name__)


from app.services.session_manager import session_manager

class WhatsAppClient:
    """
    Service stub simulating message sending via the WhatsApp Cloud API.
    """
    async def send_message(self, phone_number: str, text: str, organization_id: str) -> bool:
        """
        Simulates sending a WhatsApp text message to the specified phone number.
        """
        logger.info(f"[WhatsApp STUB] Sending message to {phone_number} (Tenant: {organization_id}): {text}")
        
        # Persist a marker key in Redis for bot_sending with 5 seconds TTL
        try:
            redis_client = await session_manager.get_client()
            bot_sending_key = f"bot_sending:{organization_id}:{phone_number}"
            await redis_client.set(bot_sending_key, "1", ex=5)
        except Exception as e:
            logger.error(f"Failed to set bot_sending key in Redis: {e}")
            
        return True



whatsapp_client = WhatsAppClient()
