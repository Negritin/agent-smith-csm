"""
Docling Service Configuration
"""

import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings for the Docling microservice."""

    # Service auth
    SERVICE_KEY: str = ""

    # Redis (broker + result backend)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Vision LLM for image descriptions
    VISION_MODEL: str = "gpt-4o-mini"
    VISION_API_URL: str = "https://api.openai.com/v1/chat/completions"
    OPENAI_API_KEY: str = ""

    # OCR engine
    OCR_ENGINE: str = "easyocr"

    # Limits
    MAX_FILE_SIZE_MB: int = 100

    # Task result TTL
    RESULT_TTL_SECONDS: int = 3600  # 1 hour

    # MinIO (shared object storage with Smith)
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "documents"
    MINIO_SECURE: bool = False

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8001

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
