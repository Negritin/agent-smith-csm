'use client';

export default function EmbedLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-screen w-screen overflow-hidden bg-transparent">
      <style jsx global>{`
        body {
          margin: 0;
          padding: 0;
          overflow: hidden;
          background: transparent !important;
        }
      `}</style>
      {children}
    </div>
  );
}
