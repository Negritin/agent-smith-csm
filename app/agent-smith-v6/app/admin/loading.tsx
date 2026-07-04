import { LoadingPage } from '@/components/ui/loading-spinner';

export default function AdminLoading() {
  return (
    <div className="p-8 w-full h-full bg-background flex flex-col gap-4">
      <div className="h-8 w-48 bg-muted rounded animate-pulse" />
      <div className="flex-1 rounded-xl bg-surface-overlay border border-border flex items-center justify-center">
        <LoadingPage />
      </div>
    </div>
  );
}
