'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    router.push('/landing');
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center bg-dark">
      <div className="text-foreground">Carregando...</div>
    </div>
  );
}
