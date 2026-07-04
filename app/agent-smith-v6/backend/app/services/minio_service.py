"""
MinIO Service - Gerenciamento de upload/download de documentos
"""

import logging
from io import BytesIO
from typing import BinaryIO, Optional

from minio import Minio
from minio.error import S3Error

from ..core.config import settings

logger = logging.getLogger(__name__)


class MinioService:
    """
    Serviço para gerenciar arquivos no MinIO (Object Storage)

    Estrutura de pastas:
    documents/
        {company_id}/
            {document_id}/
                {filename}
    """

    def __init__(self):
        self.endpoint = settings.MINIO_ENDPOINT
        self.access_key = settings.MINIO_ROOT_USER
        self.secret_key = settings.MINIO_ROOT_PASSWORD
        self.secure = settings.MINIO_SECURE
        self.bucket_name = settings.MINIO_BUCKET

        # Inicializar cliente MinIO
        self.client = Minio(
            self.endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=self.secure,
        )

        # Garantir que o bucket existe
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self) -> None:
        """Cria o bucket se não existir"""
        try:
            if not self.client.bucket_exists(self.bucket_name):
                self.client.make_bucket(self.bucket_name)
                logger.info(f"Bucket '{self.bucket_name}' criado com sucesso")
            else:
                logger.info(f"Bucket '{self.bucket_name}' já existe")
        except S3Error as e:
            logger.error(f"Erro ao verificar/criar bucket: {e}")
            raise

    def upload_file(
        self,
        file_data: BinaryIO,
        company_id: str,
        document_id: str,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        Upload de arquivo para MinIO

        Args:
            file_data: Dados do arquivo (bytes ou file-like object)
            company_id: ID da empresa (multi-tenant)
            document_id: ID do documento
            filename: Nome original do arquivo
            content_type: MIME type do arquivo

        Returns:
            Caminho do arquivo no MinIO (object_name)
        """
        # Construir caminho: documents/{company_id}/{document_id}/{filename}
        object_name = f"{company_id}/{document_id}/{filename}"

        try:
            # Se file_data não for BytesIO, converter
            if not isinstance(file_data, BytesIO):
                file_bytes = file_data.read()
                file_data = BytesIO(file_bytes)

            # Obter tamanho do arquivo
            file_data.seek(0, 2)  # Ir para o final
            file_size = file_data.tell()
            file_data.seek(0)  # Voltar para o início

            # Upload
            self.client.put_object(
                bucket_name=self.bucket_name,
                object_name=object_name,
                data=file_data,
                length=file_size,
                content_type=content_type,
            )

            logger.info(
                f"Arquivo enviado com sucesso: {object_name} ({file_size} bytes)"
            )
            return object_name

        except S3Error as e:
            logger.error(f"Erro ao fazer upload do arquivo: {e}")
            raise

    def download_file(self, object_name: str) -> BytesIO:
        """
        Download de arquivo do MinIO

        Args:
            object_name: Caminho do arquivo no MinIO

        Returns:
            BytesIO com conteúdo do arquivo
        """
        try:
            response = self.client.get_object(self.bucket_name, object_name)
            file_data = BytesIO(response.read())
            response.close()
            response.release_conn()

            logger.info(f"Arquivo baixado com sucesso: {object_name}")
            return file_data

        except S3Error as e:
            logger.error(f"Erro ao fazer download do arquivo: {e}")
            raise

    def delete_file(self, object_name: str) -> bool:
        """
        Deletar arquivo do MinIO

        Args:
            object_name: Caminho do arquivo no MinIO

        Returns:
            True se deletado com sucesso
        """
        try:
            self.client.remove_object(self.bucket_name, object_name)
            logger.info(f"Arquivo deletado com sucesso: {object_name}")
            return True

        except S3Error as e:
            logger.error(f"Erro ao deletar arquivo: {e}")
            return False

    def delete_folder(self, company_id: str, document_id: str) -> bool:
        """
        Deletar todos os arquivos de um documento

        Args:
            company_id: ID da empresa
            document_id: ID do documento

        Returns:
            True se deletado com sucesso
        """
        prefix = f"{company_id}/{document_id}/"

        try:
            objects = self.client.list_objects(
                self.bucket_name, prefix=prefix, recursive=True
            )
            for obj in objects:
                self.client.remove_object(self.bucket_name, obj.object_name)

            logger.info(f"Pasta deletada com sucesso: {prefix}")
            return True

        except S3Error as e:
            logger.error(f"Erro ao deletar pasta: {e}")
            return False

    def get_file_url(self, object_name: str, expires: int = 3600) -> str:
        """
        Gerar URL pré-assinada para download (válida por 1 hora)

        Args:
            object_name: Caminho do arquivo no MinIO
            expires: Tempo de expiração em segundos (padrão: 1 hora)

        Returns:
            URL pré-assinada
        """
        try:
            url = self.client.presigned_get_object(
                bucket_name=self.bucket_name, object_name=object_name, expires=expires
            )
            return url

        except S3Error as e:
            logger.error(f"Erro ao gerar URL pré-assinada: {e}")
            raise


# Singleton instance
_minio_service: Optional[MinioService] = None


def get_minio_service() -> MinioService:
    """Retorna instância singleton do MinioService"""
    global _minio_service
    if _minio_service is None:
        _minio_service = MinioService()
    return _minio_service
