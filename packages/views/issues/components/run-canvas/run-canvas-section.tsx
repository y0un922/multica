"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, GitBranch, Maximize2 } from "lucide-react";
import type { AgentTask } from "@multica/core/types";
import { issueTasksOptions } from "@multica/core/issues/queries";
import { cn } from "@multica/ui/lib/utils";
import { Button } from "@multica/ui/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@multica/ui/components/ui/dialog";
import { useT } from "../../../i18n";
import { isActiveCommentRun } from "../comment-runs";
import { buildDelegationForest, type DelegationForest, type DelegationNode } from "./delegation-canvas";
import { FitSurface, RunCanvas } from "./run-canvas";

/** Inline strip cap: shrink the forest to this box rather than scroll. */
const INLINE_MAX_HEIGHT = 320;

function findTask(forest: DelegationForest, id: string): AgentTask | undefined {
  const walk = (node: DelegationNode): AgentTask | undefined => {
    if (node.task.id === id) return node.task;
    for (const child of node.children) {
      const hit = walk(child);
      if (hit) return hit;
    }
  };
  for (const root of forest.roots) {
    const hit = walk(root);
    if (hit) return hit;
  }
}

/**
 * The delegation chain, inline at the top of the issue's activity column.
 *
 * Clicking a run reports it to the issue sidebar (`onInspect`). The diagram
 * itself stays a diagram — it does not grow an inspector column.
 */
export function RunChainSection({
  issueId,
  issueStatus,
  selectedId,
  onInspect,
}: {
  issueId: string;
  issueStatus?: string;
  selectedId?: string | null;
  onInspect?: (task: AgentTask | null) => void;
}) {
  const { t } = useT("issues");
  const [expanded, setExpanded] = useState(true);
  const [zoomed, setZoomed] = useState(false);
  const [uncontrolledId, setUncontrolledId] = useState<string | null>(null);
  const { data: tasks } = useQuery(issueTasksOptions(issueId));
  const forest = useMemo(() => buildDelegationForest(tasks ?? []), [tasks]);
  const currentId = selectedId !== undefined ? selectedId : uncontrolledId;

  const { runningCount, allComplete } = useMemo(() => {
    let running = 0;
    let complete = forest.runCount > 0;
    const walk = (node: DelegationNode) => {
      if (isActiveCommentRun(node.task)) running += 1;
      if (node.task.status !== "completed") complete = false;
      node.children.forEach(walk);
    };
    forest.roots.forEach(walk);
    return { runningCount: running, allComplete: complete };
  }, [forest]);

  const waitingForReview = allComplete && issueStatus === "in_progress";

  const onSelect = (id: string) => {
    const next = currentId === id ? null : id;
    if (selectedId === undefined) setUncontrolledId(next);
    onInspect?.(next ? (findTask(forest, next) ?? null) : null);
  };

  if (forest.roots.length === 0) return null;

  const graph = (maxHeight?: number, pad = "p-6") => (
    <FitSurface maxHeight={maxHeight}>
      <RunCanvas forest={forest} selectedId={currentId} onSelect={onSelect} className={pad} />
    </FitSurface>
  );

  return (
    <section className="mb-4">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-caption font-medium transition-[background-color] hover:bg-accent/70"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            "size-3 shrink-0 stroke-[2.5] text-muted-foreground transition-transform",
            expanded && "rotate-90",
          )}
        />
        <GitBranch aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
        {t(($) => $.run_canvas.section)}
        <span className="tabular-nums text-muted-foreground">
          · {t(($) => $.run_canvas.run_count, { count: forest.runCount })}
        </span>
        {runningCount > 0 ? (
          <span className="ml-1 inline-flex items-center gap-1 text-muted-foreground">
            <span className="size-1.5 animate-pulse rounded-full bg-primary" aria-hidden="true" />
            {t(($) => $.run_canvas.running, { count: runningCount })}
          </span>
        ) : waitingForReview ? (
          <span className="ml-1 text-muted-foreground">{t(($) => $.run_canvas.waiting)}</span>
        ) : null}
      </button>

      {expanded && (
        <div className="run-canvas-surface relative mt-2 overflow-hidden rounded-xl border pt-9">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="absolute right-2 top-2 z-10 text-muted-foreground"
            aria-label={t(($) => $.run_canvas.expand)}
            onClick={() => setZoomed(true)}
          >
            <Maximize2 className="size-3.5" />
          </Button>
          {!zoomed && graph(INLINE_MAX_HEIGHT)}
        </div>
      )}

      <Dialog open={zoomed} onOpenChange={setZoomed}>
        <DialogContent
          className="flex h-[min(44rem,calc(100dvh-2rem))] w-[min(72rem,calc(100vw-2rem))] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-none"
          aria-label={t(($) => $.run_canvas.expand)}
        >
          <DialogTitle className="sr-only">{t(($) => $.run_canvas.section)}</DialogTitle>
          <div className="run-canvas-surface min-h-0 flex-1">{graph(undefined, "p-8")}</div>
        </DialogContent>
      </Dialog>
    </section>
  );
}
