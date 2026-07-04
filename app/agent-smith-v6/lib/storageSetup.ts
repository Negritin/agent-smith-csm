export async function uploadVoiceMessage(audioBlob: Blob): Promise<string | null> {
  try {
    const formData = new FormData();
    formData.append('file', audioBlob, `${Date.now()}-${crypto.randomUUID()}.webm`);
    formData.append('bucket', 'voice-messages');

    const response = await fetch('/api/upload', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json();
      console.error('Error uploading audio:', error);
      return null;
    }

    const data = await response.json();
    return data.publicUrl;
  } catch (error) {
    console.error('Error in uploadVoiceMessage:', error);
    return null;
  }
}
