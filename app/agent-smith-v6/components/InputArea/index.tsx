'use client';

import { useState } from 'react';
import { AnimatedAIChat } from '../ui/animated-ai-chat';
import { ImagePreview } from './ImagePreview';
import { AudioPreview } from './AudioPreview';
import { StatusIndicator } from './StatusIndicator';
import { useImageUpload } from './hooks/useImageUpload';
import { useAudioRecorder } from './hooks/useAudioRecorder';

interface InputAreaProps {
  onSendMessage?: (message: string, imageUrl?: string) => void;
  onSendVoice?: (audioBase64: string, audioBlob: Blob) => void;
  disabled?: boolean;
  allowWebSearch?: boolean;
  onToggleWebSearch?: () => void;
  showWebSearch?: boolean;
  companyId?: string;
  agents?: { id: string; name: string }[];
  selectedAgentId?: string;
  onAgentChange?: (agentId: string) => void;
}

export default function InputArea({
  onSendMessage,
  onSendVoice,
  disabled,
  allowWebSearch = false,
  onToggleWebSearch,
  showWebSearch = true,
  companyId,
  agents = [],
  selectedAgentId = '',
  onAgentChange,
}: InputAreaProps) {
  const [message, setMessage] = useState('');

  const {
    pastedImage,
    uploadingImage,
    fileInputRef,
    handlePaste,
    handleFileSelect,
    removePastedImage,
    uploadImageToSupabase,
  } = useImageUpload({ companyId });

  const {
    pendingAudio,
    isPlaying,
    audioRef,
    isRecording,
    isProcessing,
    toggleRecording,
    handleCancelAudio,
    handleSendAudio,
    toggleAudioPreview,
    setIsPlaying,
  } = useAudioRecorder();

  const handleSend = async () => {
    let imageUrl: string | undefined = undefined;

    if (pastedImage) {
      imageUrl = (await uploadImageToSupabase(pastedImage.file)) || undefined;
      removePastedImage();
    }

    if ((message.trim() || imageUrl) && onSendMessage) {
      onSendMessage(message.trim() || '[Imagem]', imageUrl);
      setMessage('');
    }
  };

  const placeholder = isRecording
    ? 'Gravando...'
    : isProcessing
      ? 'Processando...'
      : 'Digite sua mensagem...';

  return (
    <div className="w-full max-w-5xl mx-auto px-4 pb-6">
      {pastedImage && (
        <ImagePreview
          imageUrl={pastedImage.url}
          uploading={uploadingImage}
          onRemove={removePastedImage}
        />
      )}

      {pendingAudio ? (
        <AudioPreview
          audioUrl={pendingAudio.url}
          isPlaying={isPlaying}
          audioRef={audioRef}
          onCancel={handleCancelAudio}
          onSend={() => onSendVoice && handleSendAudio(onSendVoice)}
          onTogglePlay={toggleAudioPreview}
          onEnded={() => setIsPlaying(false)}
        />
      ) : (
        <AnimatedAIChat
          value={message}
          onChange={setMessage}
          onSend={handleSend}
          onVoiceRecord={toggleRecording}
          onPaste={handlePaste}
          isRecording={isRecording}
          isTyping={disabled || uploadingImage}
          placeholder={placeholder}
          disabled={disabled || isProcessing || uploadingImage}
          showWebSearch={showWebSearch}
          allowWebSearch={allowWebSearch}
          onToggleWebSearch={onToggleWebSearch}
          onFileSelect={() => fileInputRef.current?.click()}
          agents={agents}
          selectedAgentId={selectedAgentId}
          onAgentChange={onAgentChange}
        />
      )}

      <input
        type="file"
        ref={fileInputRef}
        className="hidden"
        accept="image/*"
        onChange={handleFileSelect}
      />

      <StatusIndicator
        isRecording={isRecording}
        isProcessing={isProcessing}
        uploadingImage={uploadingImage}
      />
    </div>
  );
}
