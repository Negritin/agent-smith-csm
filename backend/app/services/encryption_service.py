"""
Encryption Service - Criptografa/descriptografa API keys dos LLM providers
"""

import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet

from app.core.config import settings

logger = logging.getLogger(__name__)


class EncryptionService:
    """
    Serviço para criptografar e descriptografar dados sensíveis (API keys).

    Usa AES via biblioteca cryptography (Fernet).
    Chave vem da variável de ambiente ENCRYPTION_KEY.
    """

    def __init__(self):
        """Inicializa o serviço de criptografia"""
        encryption_key = settings.ENCRYPTION_KEY

        if not encryption_key:
            raise ValueError(
                "ENCRYPTION_KEY environment variable is required. "
                'Generate one with: python -c "import secrets, base64; '
                'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
            )

        try:
            # A chave já deve estar em base64 URL-safe
            # Se não estiver, converte para URL-safe
            try:
                # Tenta decodificar e recodificar como URL-safe
                key_bytes = base64.b64decode(encryption_key)
                url_safe_key = base64.urlsafe_b64encode(key_bytes)
            except Exception:
                # Se falhar, assume que já é URL-safe
                url_safe_key = encryption_key.encode("utf-8")

            self.cipher = Fernet(url_safe_key)
            logger.info("Encryption service initialized successfully")
        except Exception as e:
            raise ValueError(f"Invalid ENCRYPTION_KEY format: {e}") from e

    def encrypt(self, plaintext: str) -> str:
        """
        Criptografa um texto

        Args:
            plaintext: Texto a ser criptografado (ex: API key)

        Returns:
            Texto criptografado em base64
        """
        if not plaintext:
            raise ValueError("plaintext cannot be empty")

        try:
            encrypted_bytes = self.cipher.encrypt(plaintext.encode("utf-8"))
            encrypted_b64 = base64.b64encode(encrypted_bytes).decode("utf-8")
            return encrypted_b64
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise

    def decrypt(self, ciphertext: str) -> str:
        """
        Descriptografa um texto

        Args:
            ciphertext: Texto criptografado (base64)

        Returns:
            Texto original descriptografado
        """
        if not ciphertext:
            raise ValueError("ciphertext cannot be empty")

        try:
            encrypted_bytes = base64.b64decode(ciphertext)
            decrypted_bytes = self.cipher.decrypt(encrypted_bytes)
            decrypted_text = decrypted_bytes.decode("utf-8")

            logger.debug("Successfully decrypted data")
            return decrypted_text
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise ValueError(f"Failed to decrypt: {e}") from e


# Singleton instance
_encryption_service: Optional[EncryptionService] = None


def get_encryption_service() -> EncryptionService:
    """Retorna instância singleton do EncryptionService"""
    global _encryption_service
    if _encryption_service is None:
        _encryption_service = EncryptionService()
    return _encryption_service
