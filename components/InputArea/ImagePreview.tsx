'use client';

import { X } from 'lucide-react';

interface ImagePreviewProps {
  imageUrl: string;
  uploading: boolean;
  onRemove: () => void;
}

export function ImagePreview({ imageUrl, uploading, onRemove }: ImagePreviewProps) {
  return (
    <div className="mb-3 relative inline-block">
      <div className="relative group">
        <img
          src={imageUrl}
          alt="Preview"
          className="max-h-32 rounded-lg border-2 border-primary/30 shadow-lg"
        />
        <button
          onClick={onRemove}
          className="absolute -top-2 -right-2 bg-danger hover:bg-danger/90 text-danger-foreground rounded-full p-1 shadow-lg transition-all"
          title="Remover imagem"
        >
          <X className="h-4 w-4" />
        </button>
        {uploading && (
          <div className="absolute inset-0 bg-foreground/40 flex items-center justify-center rounded-lg">
            <div className="text-primary-foreground text-sm">Enviando...</div>
          </div>
        )}
      </div>
    </div>
  );
}
