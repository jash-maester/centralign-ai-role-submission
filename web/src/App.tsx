import { lazy, Suspense, useEffect, useRef } from 'react';
import { startPolling, useLedger } from './store/store';
import { TopBar } from './components/TopBar';
import { Dashboard } from './views/dashboard/Dashboard';
import { StepDrawer } from './views/dashboard/StepDrawer';
import { AgentSheet } from './views/dashboard/AgentSheet';

const ReportView = lazy(() => import('./views/report/Report'));
const Builder = lazy(() => import('./views/builder/Builder'));

function Toasts() {
  const toasts = useLedger((s) => s.toasts);
  const dismiss = useLedger((s) => s.dismissToast);
  return (
    <div data-noprint className="fixed bottom-4 left-1/2 -translate-x-1/2 z-30 flex flex-col gap-2 items-center">
      {toasts.map((t) => (
        <button key={t.id} type="button" onClick={() => dismiss(t.id)} className="px-3.5 py-2 rounded-lg border shadow-card text-sm+ bg-panel" style={{ borderColor: t.tone === 'error' ? 'var(--s-rejected)' : 'var(--line2)', color: t.tone === 'error' ? 'var(--s-rejected)' : 'var(--fg)' }}>
          {t.text}
        </button>
      ))}
    </div>
  );
}

export function App() {
  const view = useLedger((s) => s.view);
  const theme = useLedger((s) => s.theme);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    void useLedger.getState().init();
    return startPolling();
  }, []);

  const toNeeds = () => {
    useLedger.getState().setView('dashboard');
    requestAnimationFrame(() => scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' }));
  };

  // Dashboard and Builder stay mounted; switching views only toggles display.
  return (
    <>
      <div className="h-screen flex-col print-plain" style={{ display: view !== 'builder' ? 'flex' : 'none' }}>
        <TopBar onNeeds={toNeeds} />
        <div ref={scrollRef} className="flex-1 min-h-0 overflow-auto print-plain">
          <div style={{ display: view === 'dashboard' ? 'block' : 'none' }}>
            <Dashboard />
          </div>
          {view === 'report' && (
            <Suspense fallback={<div className="p-10 text-fg3">Loading report…</div>}>
              <ReportView />
            </Suspense>
          )}
        </div>
      </div>
      <div className="h-screen" style={{ display: view === 'builder' ? 'block' : 'none' }} data-noprint>
        <Suspense fallback={<div className="p-10 text-fg3">Loading builder…</div>}>
          <Builder active={view === 'builder'} onNeeds={toNeeds} />
        </Suspense>
      </div>
      <StepDrawer />
      <AgentSheet />
      <Toasts />
    </>
  );
}
