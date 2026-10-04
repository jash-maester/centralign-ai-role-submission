/** Step graph: React Flow + dagre, coloured by step state (plans/04 Prompt 2). */
import { memo, useMemo } from 'react';
import dagre from '@dagrejs/dagre';
import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useLedger } from '../../store/store';
import { LEGEND, stepVisual, VISUAL_COLOR } from '../../lib/states';
import { agentView, buildLanes } from '../../lib/derive';
import { TtlRing, useNow } from '../../components/ui';
import type { Step } from '../../api/types';

const W = 156, H = 64;

interface NodeData extends Record<string, unknown> {
  step: Step;
  lead: string;
  ttl: number | null;
}

const StepNode = memo(function StepNode({ data }: NodeProps<Node<NodeData>>) {
  const { step, lead, ttl } = data;
  const vis = stepVisual(step.status, { skipped: step.status === 'replanned' });
  const hist = step.history ?? [];
  const prev = hist.filter((a) => a.outcome === 'lease_expired' && a.worker && a.worker !== step.lease_owner).map((a) => a.worker!);
  const agent = step.lease_owner ?? hist[hist.length - 1]?.worker ?? (step.status === 'input_required' ? 'human' : '');
  const quiet = vis.v === 'planned' || vis.v === 'skipped';
  return (
    <div
      className="bg-panel rounded-lg px-2.5 py-2 flex flex-col gap-[5px] text-fg cursor-pointer hover:bg-panel2"
      style={{ width: W, height: H, border: `1px solid ${quiet ? 'var(--line)' : `color-mix(in oklch, ${vis.c} 45%, var(--line))`}` }}
      title={`${step.id} · ${lead}`}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <span className="flex justify-between gap-1.5 mono text-2xs">
        <span className="truncate">{step.kind.replace('crm.', '').replace('review.', 'review·')}</span>
        <span style={{ color: step.attempt > 1 ? 'var(--s-claimed)' : 'var(--fg3)' }}>a{Math.max(1, step.attempt)}</span>
      </span>
      <span className="flex items-center gap-1.5 text-xs font-medium" style={{ color: vis.c }}>
        <span className={`flex-none w-[7px] h-[7px] rounded-[2px] ${vis.v === 'leased' ? 'pulse' : ''}`} style={{ background: vis.c }} />
        {vis.label}
        <span className="flex-1" />
        <TtlRing frac={ttl} size={12} />
      </span>
      <span className="flex gap-[5px] mono text-2xs text-fg3 min-w-0 whitespace-nowrap overflow-hidden">
        {prev.map((p) => <span key={p} className="line-through">{p.replace('worker-', '')}</span>)}
        <span className="truncate">{agent ? agent.replace('worker-', '') : lead}</span>
      </span>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
});

const nodeTypes = { step: StepNode };

function layout(steps: Step[]): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 12, ranksep: 46, marginx: 10, marginy: 10 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(steps.map((s) => s.id));
  steps.forEach((s) => g.setNode(s.id, { width: W, height: H }));
  steps.forEach((s) => (s.depends_on ?? []).forEach((d) => ids.has(d) && g.setEdge(d, s.id)));
  dagre.layout(g);
  const out = new Map<string, { x: number; y: number }>();
  steps.forEach((s) => {
    const n = g.node(s.id);
    if (n) out.set(s.id, { x: n.x - W / 2, y: n.y - H / 2 });
  });
  return out;
}

export default function StepGraph() {
  const steps = useLedger((s) => s.steps);
  const order = useLedger((s) => s.stepOrder);
  const facts = useLedger((s) => s.facts);
  const agents = useLedger((s) => s.agents);
  const agentsAt = useLedger((s) => s.agentsAt);
  const ttlS = useLedger((s) => s.config.lease_ttl_s);
  const openStep = useLedger((s) => s.openStep);
  const now = useNow(1000);

  const list = useMemo(() => order.map((id) => steps[id]).filter(Boolean), [order, steps]);
  // Layout depends on the graph shape only, not on statuses.
  const shapeKey = useMemo(() => list.map((s) => `${s.id}<${(s.depends_on ?? []).join(',')}`).join('|'), [list]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const pos = useMemo(() => layout(list), [shapeKey]);
  const leadName = useMemo(() => {
    const m = new Map<string, string>();
    buildLanes(steps, order, facts).forEach((l) => m.set(l.lane, l.lead.name));
    return m;
  }, [steps, order, facts]);
  const ttlByStep = useMemo(() => {
    const m = new Map<string, number | null>();
    agents.forEach((a) => {
      if (a.current_step) m.set(a.current_step, agentView(a, now, agentsAt, ttlS, 0).ttlFrac);
    });
    return m;
  }, [agents, agentsAt, now, ttlS]);

  const nodes: Node<NodeData>[] = list.map((s) => ({
    id: s.id,
    type: 'step',
    position: pos.get(s.id) ?? { x: 0, y: 0 },
    data: { step: s, lead: s.lane ? leadName.get(s.lane) ?? s.lane : 'run', ttl: ttlByStep.get(s.id) ?? null },
    draggable: false,
  }));
  const edges: Edge[] = list.flatMap((s) => (s.depends_on ?? []).filter((d) => steps[d]).map((d) => {
    const live = ['leased', 'claimed_done', 'verified'].includes(s.status);
    return { id: `${d}>${s.id}`, source: d, target: s.id, animated: live, style: { stroke: live ? stepVisual(s.status).c : 'var(--line2)', strokeWidth: live ? 1.6 : 1 } };
  }));

  return (
    <div className="px-[18px] pt-4 pb-5 flex flex-col gap-2" data-testid="step-graph">
      <div className="flex gap-3.5 flex-wrap pb-1 mono text-2xs text-fg2">
        {LEGEND.map((l) => (
          <span key={l.v} className="inline-flex items-center gap-[5px]"><span className="w-2 h-2 rounded-[2px]" style={{ background: VISUAL_COLOR[l.v] }} />{l.label}</span>
        ))}
        <span className="inline-flex items-center gap-[5px]"><TtlRing frac={0.66} size={11} />lease TTL</span>
        <span className="text-fg3">· amber = claimed, not yet proven · green only after the verifier commits</span>
      </div>
      <div className="h-[560px] border border-line rounded-lg bg-panel2">
        {list.length === 0 ? (
          <div className="h-full grid place-items-center text-fg3 text-sm+">No steps yet. The graph appears when the orchestrator plans.</div>
        ) : (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodeClick={(_, n) => openStep(n.id)}
            fitView
            fitViewOptions={{ padding: 0.08 }}
            minZoom={0.2}
            maxZoom={1.6}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background color="var(--dot)" gap={20} />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>
    </div>
  );
}
