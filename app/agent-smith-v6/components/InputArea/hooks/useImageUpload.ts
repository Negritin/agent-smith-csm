'use client';

import { useState, useRef, useCallback } from 'react';

interface UseImageUploadProps {
  companyId?: string;
}

interface UseImageUploadReturn {
  pastedImage: { url: string; file: File } | null;
  uploadingImage: boolean;
  fileInputRef: React.RefObject<HTMLInputElement>;
  handlePaste: (event: React.ClipboardEvent) => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  removePastedImage: () => void;
  uploadImageToSupabase: (file: File) => Promise<string | null>;
}

export function useImageUpload({ companyId }: UseImageUploadProps): UseImageUploadReturn {
  const [pastedImage, setPastedImage] = useState<{ url: string; file: File } | null>(null);
  const [uploadingImage, setUploadingImage] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handlePaste = useCallback((event: React.ClipboardEvent) => {
    const items = event.clipboardData.items;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.indexOf('image') !== -1) {
        event.preventDefault();
        const file = items[i].getAsFile();
        if (file) {
          const previewUrl = URL.createObjectURL(file);
          setPastedImage({ url: previewUrl, file });
        }
        break;
      }
    }
  }, []);

  const removePastedImage = useCallback(() => {
    if (pastedImage) {
      URL.revokeObjectURL(pastedImage.url);
      setPastedImage(null);
    }
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  }, [pastedImage]);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      const file = e.target.files[0];
      if (file.type.startsWith('image/')) {
        const previewUrl = URL.createObjectURL(file);
        setPastedImage({ url: previewUrl, file });
      }
    }
  }, []);

  const uploadImageToSupabase = useCallback(
    async (file: File): Promise<string | null> => {
      if (!companyId) {
        console.error('[VISION] companyId required for upload');
        return null;
      }

      try {
        setUploadingImage(true);
        const formData = new FormData();
        formData.append('file', file);
        formData.append('bucket', 'chat-media');
        formData.append('path', `${companyId}/${new Date().toISOString().split('T')[0]}`);

        const response = await fetch('/api/upload', {
          method: 'POST',
          body: formData,
        });

        if (!response.ok) {
          const error = await response.json();
          console.error('[VISION] Upload error:', error);
          return null;
        }

        const data = await response.json();
        return data.publicUrl;
      } catch (error) {
        console.error('[VISION] Upload failed:', error);
        return null;
      } finally {
        setUploadingImage(false);
      }
    },
    [companyId],
  );

  return {
    pastedImage,
    uploadingImage,
    fileInputRef,
    handlePaste,
    handleFileSelect,
    removePastedImage,
    uploadImageToSupabase,
  };
}
