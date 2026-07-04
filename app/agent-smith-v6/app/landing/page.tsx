'use client';

import { useEffect } from 'react';
import { useTheme } from 'next-themes';
import { HeroSection } from '@/components/HeroSection';

export default function LandingPage() {
  const { setTheme, theme } = useTheme();

  // Force dark mode on mount, restore on unmount
  useEffect(() => {
    const prev = theme;
    setTheme('dark');
    return () => {
      if (prev && prev !== 'dark') {
        setTheme(prev);
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="landing-legacy-theme">
      <HeroSection />
    </div>
  );
}
