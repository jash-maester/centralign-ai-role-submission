import { useLedger } from '../../store/store';
import { NeedsAttention } from './NeedsAttention';
import { RunStrip } from './RunStrip';
import { Workflow } from './Workflow';
import { RunControls } from './RunControls';
import { Criteria } from './Criteria';
import { LiveLedger } from './LiveLedger';
import { RecentRuns } from './RecentRuns';

export function Dashboard() {
  const runId = useLedger((s) => s.runId);
  const loading = useLedger((s) => s.loading);
  const setView = useLedger((s) => s.setView);
  return (
    <main className="w-full max-w-[1360px] mx-auto px-7 pt-6 pb-12 flex flex-col gap-5" data-testid="dashboard">
      <NeedsAttention />
      {runId ? (
        <>
          <RunStrip />
          <Workflow />
          <RunControls />
          <section className="grid gap-4 items-start" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.4fr)' }}>
            <Criteria />
            <LiveLedger />
          </section>
        </>
      ) : (
        <section className="bg-panel border border-line rounded-[10px] px-5 py-6 shadow-card flex flex-col gap-2">
          <span className="text-md font-semibold">{loading ? 'Loading…' : 'No run selected'}</span>
          <span className="text-sm+ text-fg2">Start a run from the Builder: type a goal and press Run. It appears here live.</span>
          <button type="button" onClick={() => setView('builder')} className="self-start border-0 bg-transparent p-0 text-accent text-sm+">Open the builder →</button>
        </section>
      )}
      <RecentRuns />
    </main>
  );
}
