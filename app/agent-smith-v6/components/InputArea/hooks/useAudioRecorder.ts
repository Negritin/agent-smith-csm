'use client';

import { useState, useRef, useCallback } from 'react';

type RecordingState = 'idle' | 'recording' | 'processing';

interface PendingAudio {
  blob: Blob;
  base64: string;
  url: string;
}

interface UseAudioRecorderReturn {
  recordingState: RecordingState;
  pendingAudio: PendingAudio | null;
  isPlaying: boolean;
  audioRef: React.RefObject<HTMLAudioElement>;
  isRecording: boolean;
  isProcessing: boolean;
  toggleRecording: () => void;
  handleCancelAudio: () => void;
  handleSendAudio: (onSendVoice: (base64: string, blob: Blob) => void) => void;
  toggleAudioPreview: () => void;
  setIsPlaying: (value: boolean) => void;
}

export function useAudioRecorder(): UseAudioRecorderReturn {
  const [recordingState, setRecordingState] = useState<RecordingState>('idle');
  const [pendingAudio, setPendingAudio] = useState<PendingAudio | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const startRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };

      mediaRecorder.onstop = async () => {
        setRecordingState('processing');
        const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        const audioUrl = URL.createObjectURL(audioBlob);

        const reader = new FileReader();
        reader.onloadend = () => {
          const base64 = reader.result as string;
          const base64Data = base64.split(',')[1];
          setPendingAudio({ blob: audioBlob, base64: base64Data, url: audioUrl });
          setRecordingState('idle');
        };
        reader.readAsDataURL(audioBlob);

        stream.getTracks().forEach((track) => track.stop());
      };

      mediaRecorder.start();
      setRecordingState('recording');
    } catch (error) {
      console.error('Error accessing microphone:', error);
      alert('Não foi possível acessar o microfone. Verifique as permissões.');
      setRecordingState('idle');
    }
  }, []);

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
      mediaRecorderRef.current.stop();
    }
  }, []);

  const toggleRecording = useCallback(() => {
    if (recordingState === 'idle') {
      startRecording();
    } else if (recordingState === 'recording') {
      stopRecording();
    }
  }, [recordingState, startRecording, stopRecording]);

  const handleCancelAudio = useCallback(() => {
    if (pendingAudio) {
      URL.revokeObjectURL(pendingAudio.url);
      setPendingAudio(null);
      setIsPlaying(false);
    }
  }, [pendingAudio]);

  const handleSendAudio = useCallback(
    (onSendVoice: (base64: string, blob: Blob) => void) => {
      if (pendingAudio) {
        onSendVoice(pendingAudio.base64, pendingAudio.blob);
        URL.revokeObjectURL(pendingAudio.url);
        setPendingAudio(null);
        setIsPlaying(false);
      }
    },
    [pendingAudio],
  );

  const toggleAudioPreview = useCallback(() => {
    if (audioRef.current) {
      if (isPlaying) {
        audioRef.current.pause();
      } else {
        audioRef.current.play();
      }
      setIsPlaying(!isPlaying);
    }
  }, [isPlaying]);

  return {
    recordingState,
    pendingAudio,
    isPlaying,
    audioRef,
    isRecording: recordingState === 'recording',
    isProcessing: recordingState === 'processing',
    toggleRecording,
    handleCancelAudio,
    handleSendAudio,
    toggleAudioPreview,
    setIsPlaying,
  };
}
