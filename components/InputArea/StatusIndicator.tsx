'use client';

interface StatusIndicatorProps {
  isRecording: boolean;
  isProcessing: boolean;
  uploadingImage: boolean;
}

export function StatusIndicator({
  isRecording,
  isProcessing,
  uploadingImage,
}: StatusIndicatorProps) {
  if (isRecording) {
    return (
      <p className="text-center text-sm text-primary mt-2 animate-pulse">
        Gravando... Clique no botão novamente para parar
      </p>
    );
  }

  if (isProcessing) {
    return <p className="text-center text-sm text-warning mt-2">Processando áudio...</p>;
  }

  if (uploadingImage) {
    return <p className="text-center text-sm text-accent mt-2">Enviando imagem...</p>;
  }

  return null;
}
