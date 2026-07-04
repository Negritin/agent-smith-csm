"""
Serviço de Áudio - Whisper API da OpenAI (ASYNC)
"""

import asyncio
import base64
import logging
import os
import tempfile

import httpx
from openai import AsyncOpenAI

from app.core.security.url_validator import (
    ExternalUrlValidationError,
    revalidate_external_url,
    validate_external_url,
)

logger = logging.getLogger(__name__)

# Cap de tamanho do áudio inbound (F05): aplicado via streaming, sem
# bufferizar o corpo inteiro. Áudios do WhatsApp são pequenos; 25 MB é o
# teto da própria API Whisper, então é um limite seguro e generoso.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class AudioService:
    """Serviço para transcrever áudio usando Whisper API (async)"""

    def __init__(self, openai_api_key: str):
        """
        Inicializa o serviço de áudio

        Args:
            openai_api_key: API key da OpenAI
        """
        self.client = AsyncOpenAI(api_key=openai_api_key)
        logger.info("Audio service initialized with Whisper API (async)")

    async def transcribe_audio(
        self,
        audio_base64: str,
        company_id: str = None,
        agent_id: str = None
    ) -> str:
        """
        Transcreve áudio em base64 usando Whisper API (async).
        Não bloqueia o event loop durante a chamada à API.

        Args:
            audio_base64: Áudio em formato base64
            company_id: ID da empresa (para billing)
            agent_id: ID do agente (para billing)

        Returns:
            Texto transcrito

        Raises:
            ValueError: Se o áudio estiver vazio ou inválido
            Exception: Se houver erro na transcrição
        """
        try:
            if not audio_base64:
                raise ValueError("Audio data is empty")

            logger.info("[AUDIO] Starting audio transcription")

            # Decodificar base64 (CPU-bound, rápido, ok ser sync)
            audio_bytes = base64.b64decode(audio_base64)
            logger.info(f"[AUDIO] Decoded audio size: {len(audio_bytes)} bytes")

            # Criar arquivo temporário (I/O local, rápido, ok ser sync)
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temp_file:
                temp_file.write(audio_bytes)
                temp_file_path = temp_file.name

            try:
                logger.info(f"[AUDIO] Sending to Whisper API: {temp_file_path}")

                with open(temp_file_path, "rb") as audio_file:
                    # ✅ ASYNC: await na chamada à API da OpenAI
                    transcript = await self.client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="pt",
                        response_format="verbose_json",
                    )

                transcribed_text = transcript.text

                # Track cost (sync, rápido, ok por enquanto)
                try:
                    duration_seconds = getattr(transcript, "duration", None)
                    if duration_seconds:
                        from .usage_service import get_usage_service

                        usage_service = get_usage_service()
                        usage_service.track_cost_sync(
                            service_type="audio",
                            model="whisper-1",
                            input_tokens=int(duration_seconds),
                            output_tokens=0,
                            company_id=company_id,
                            agent_id=agent_id,
                            details={"duration_seconds": duration_seconds},
                        )
                except Exception as e:
                    logger.warning(f"[AUDIO] Cost tracking failed: {e}")

                logger.info(
                    f"[AUDIO] Transcription successful: {transcribed_text[:100]}..."
                )

                return transcribed_text

            finally:
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                    logger.debug(f"[AUDIO] Temporary file deleted: {temp_file_path}")

        except ValueError as e:
            logger.error(f"[AUDIO] Validation error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"[AUDIO] Transcription error: {str(e)}", exc_info=True)
            raise Exception(f"Failed to transcribe audio: {str(e)}") from e

    async def transcribe_audio_from_url(
        self,
        audio_url: str,
        company_id: str = None,
        agent_id: str = None
    ) -> str:
        """
        Transcreve áudio a partir de URL (async).
        Usado para WhatsApp.

        Args:
            audio_url: URL do áudio
            company_id: ID da empresa (para billing)
            agent_id: ID do agente (para billing)

        Returns:
            Texto transcrito

        Raises:
            ValueError: Se a URL estiver vazia ou inválida
            Exception: Se houver erro no download ou transcrição
        """
        try:
            if not audio_url:
                raise ValueError("Audio URL is empty")

            logger.info(f"[AUDIO] Downloading audio from URL: {audio_url[:100]}...")

            # SSRF guard (F05): valida a URL (atacante-controlada via webhook)
            # ANTES do GET. validate_external_url faz DNS bloqueante, então é
            # offloaded via asyncio.to_thread para não bloquear o event loop.
            validated = await asyncio.to_thread(validate_external_url, audio_url)

            # ✅ ASYNC: download via streaming, follow_redirects=False, com cap
            # de tamanho aplicado ANTES de bufferizar o corpo inteiro.
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=False
            ) as http_client:
                revalidate_external_url(validated)
                chunks: list[bytes] = []
                total = 0
                async with http_client.stream(
                    "GET", validated.normalized_url
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "")
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_AUDIO_BYTES:
                            await response.aclose()
                            raise ExternalUrlValidationError("Audio exceeds size cap")
                        chunks.append(chunk)
            audio_bytes = b"".join(chunks)
            logger.info(f"[AUDIO] Downloaded audio size: {len(audio_bytes)} bytes")

            extension_map = {
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/wav": ".wav",
                "audio/webm": ".webm",
            }

            extension = extension_map.get(content_type, ".ogg")

            logger.info(f"[AUDIO] Detected format: {content_type} -> {extension}")

            with tempfile.NamedTemporaryFile(
                suffix=extension, delete=False
            ) as temp_file:
                temp_file.write(audio_bytes)
                temp_file_path = temp_file.name

            try:
                logger.info(f"[AUDIO] Sending to Whisper API: {temp_file_path}")

                with open(temp_file_path, "rb") as audio_file:
                    # ✅ ASYNC: await na chamada à API
                    transcript = await self.client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="pt",
                    )

                transcribed_text = transcript.text

                try:
                    duration_seconds = getattr(transcript, "duration", None)
                    if duration_seconds and company_id:
                        from .usage_service import get_usage_service

                        usage_service = get_usage_service()
                        usage_service.track_cost_sync(
                            service_type="audio",
                            model="whisper-1",
                            input_tokens=int(duration_seconds),
                            output_tokens=0,
                            company_id=company_id,
                            agent_id=agent_id,
                            details={"duration_seconds": duration_seconds, "source": "whatsapp"},
                        )
                        logger.info(f"[AUDIO] Billing tracked: {duration_seconds}s for company {company_id}")
                except Exception as e:
                    logger.warning(f"[AUDIO] Cost tracking failed: {e}")

                logger.info(
                    f"[AUDIO] Transcription successful: {transcribed_text[:100]}..."
                )

                return transcribed_text

            finally:
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                    logger.debug(f"[AUDIO] Temporary file deleted: {temp_file_path}")

        except ExternalUrlValidationError as e:
            logger.warning(f"[AUDIO] Blocked audio URL (SSRF/size policy): {e}")
            raise Exception(f"Audio URL blocked by security policy: {str(e)}") from e
        except httpx.HTTPError as e:
            logger.error(f"[AUDIO] Error downloading audio: {str(e)}")
            raise Exception(f"Failed to download audio from URL: {str(e)}") from e
        except ValueError as e:
            logger.error(f"[AUDIO] Validation error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"[AUDIO] Transcription error: {str(e)}", exc_info=True)
            raise Exception(f"Failed to transcribe audio from URL: {str(e)}") from e
