import type { AgentTask } from "@multica/core/types";

/**
 * One run on the canvas plus the runs it delegated to, which render beside it.
 */
export interface DelegationNode {
  task: AgentTask;
  children: DelegationNode[];
}

export interface DelegationForest {
  /** Only trees that carry at least one edge — see `buildDelegationForest`. */
  roots: DelegationNode[];
  /** Runs placed in the forest, roots included. */
  runCount: number;
  /** Edges drawn, i.e. runs that sit under a parent. */
  edgeCount: number;
}

function rawParentId(task: AgentTask): string | null {
  const parent = task.attribution?.delegated_from_task_id;
  return parent ? parent : null;
}

/**
 * True when the edge `task -> parentId` would put `task` inside a cycle, i.e.
 * when following parents from `parentId` leads back to `task`. `delegated_from_task_id`
 * is copied rather than chained (MUL-4302 §3.2), so a run can only ever point at
 * an older one and a real payload cannot loop — but a hand-edited row can, and
 * an unguarded walk would hang the render.
 *
 * The check has to ask whether the loop CONTAINS `task`, not whether one exists
 * somewhere up the chain: a run that merely delegates to the head of a loop it
 * is not part of still has a real parent, and dropping that edge would delete a
 * genuine hand-off from the canvas.
 */
function edgeClosesLoop(
  task: AgentTask,
  parentId: string,
  byId: ReadonlyMap<string, AgentTask>,
): boolean {
  const seen = new Set<string>();
  let cursor: string | null = parentId;
  while (cursor) {
    if (cursor === task.id) return true;
    // A loop that never reaches `task` cannot be closed by this edge, and
    // bailing here is also what stops the walk on that loop.
    if (seen.has(cursor)) return false;
    seen.add(cursor);
    const node = byId.get(cursor);
    cursor = node ? rawParentId(node) : null;
  }
  return false;
}

function byCreatedAt(a: AgentTask, b: AgentTask): number {
  const created = Date.parse(a.created_at) - Date.parse(b.created_at);
  return (Number.isFinite(created) ? created : 0) || a.id.localeCompare(b.id);
}

/**
 * Group an issue's runs into delegation trees, parent above delegated child.
 *
 * The edge is `attribution.delegated_from_task_id`: the run whose accountable
 * human was COPIED onto this one — an agent `@`-mentioning another agent, or an
 * agent filing a sub-issue. It is the only field on `AgentTask` that records
 * "run A caused run B", so it is the whole multi-agent chain. `parent_task_id`
 * is deliberately NOT an edge: that is the same agent auto-retrying itself, and
 * drawing it would claim a hand-off where there was only a second attempt.
 *
 * Runs that touch no edge are dropped. A standalone run is the execution log's
 * story; on the canvas it would be a card with no connectors, which reads as a
 * broken chain rather than as a chain of one.
 */
export function buildDelegationForest(tasks: readonly AgentTask[]): DelegationForest {
  const byId = new Map(tasks.map((task) => [task.id, task]));

  // Resolve each run's parent to a run we actually hold, dropping edges that
  // point outside this issue (a run delegated across issues is not drawn on
  // this issue's canvas) and edges that would close a loop.
  const parentOf = new Map<string, string | null>();
  for (const task of tasks) {
    const parent = rawParentId(task);
    parentOf.set(
      task.id,
      parent && byId.has(parent) && !edgeClosesLoop(task, parent, byId) ? parent : null,
    );
  }

  const childrenOf = new Map<string, AgentTask[]>();
  const rootTasks: AgentTask[] = [];
  for (const task of tasks) {
    const parent = parentOf.get(task.id) ?? null;
    if (!parent) {
      rootTasks.push(task);
      continue;
    }
    const siblings = childrenOf.get(parent);
    if (siblings) siblings.push(task);
    else childrenOf.set(parent, [task]);
  }

  const build = (task: AgentTask): DelegationNode => ({
    task,
    children: (childrenOf.get(task.id) ?? []).toSorted(byCreatedAt).map(build),
  });

  const roots = rootTasks
    .toSorted(byCreatedAt)
    .map(build)
    .filter((root) => root.children.length > 0);

  let runCount = 0;
  let edgeCount = 0;
  const tally = (node: DelegationNode) => {
    runCount += 1;
    edgeCount += node.children.length;
    node.children.forEach(tally);
  };
  roots.forEach(tally);

  return { roots, runCount, edgeCount };
}

/**
 * A card's measured box, relative to the canvas container's top-left corner.
 */
export interface NodeBox {
  left: number;
  right: number;
  centerY: number;
}

/** Corner rounding on a connector's turn, in px. */
const CORNER_RADIUS = 8;

/** Keep path strings stable across subpixel jitter so a re-measure can no-op. */
const px = (value: number) => Math.round(value * 10) / 10;

/**
 * SVG path data for every connector between one card and its children.
 *
 * All children of a card share one vertical spine at the midpoint of the gap,
 * because they are laid out in the same `grid` column. The two outermost
 * children get the rounded corners — the spine starts and ends exactly on their
 * centre lines, never above the first or below the last — and the ones between
 * them get a bare horizontal stub off the spine.
 *
 * Each child's own stub is drawn here too, at its centre line, which is what
 * makes the card's height irrelevant: a taller card moves its own stub, and the
 * spine is derived from the outermost two, so nothing needs a fixed card height.
 */
export function connectorPaths(
  parent: NodeBox,
  children: readonly NodeBox[],
): string[] {
  if (children.length === 0) return [];

  const ordered = children.toSorted((a, b) => a.centerY - b.centerY);
  const first = ordered[0]!;
  const last = ordered[ordered.length - 1]!;
  // Shared by every child of this parent by construction; `min` only defends
  // against a subpixel difference between siblings.
  const spineX = px((parent.right + Math.min(...ordered.map((c) => c.left))) / 2);
  const paths = [`M ${px(parent.right)} ${px(parent.centerY)} H ${spineX}`];

  if (ordered.length === 1) {
    paths.push(`M ${spineX} ${px(first.centerY)} H ${px(first.left)}`);
    return paths;
  }

  const radius = px(
    Math.max(
      0,
      Math.min(
        CORNER_RADIUS,
        (last.centerY - first.centerY) / 2,
        first.left - spineX,
      ),
    ),
  );
  paths.push(
    `M ${px(first.left)} ${px(first.centerY)}` +
      ` H ${px(spineX + radius)}` +
      ` Q ${spineX} ${px(first.centerY)} ${spineX} ${px(first.centerY + radius)}` +
      ` V ${px(last.centerY - radius)}` +
      ` Q ${spineX} ${px(last.centerY)} ${px(spineX + radius)} ${px(last.centerY)}` +
      ` H ${px(last.left)}`,
  );
  for (const child of ordered.slice(1, -1)) {
    paths.push(`M ${spineX} ${px(child.centerY)} H ${px(child.left)}`);
  }

  return paths;
}
