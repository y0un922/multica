"use client";

import { useEffect, useMemo, useState } from "react";
import { Brain, ChevronDown, ChevronLeft, FileText, Search, SquarePen, Terminal } from "lucide-react";
import type { AgentTask } from "@multica/core/types";
import { useTaskMessages } from "@multica/core/chat/queries";
import { useActorName } from "@multica/core/workspace/hooks";
import { cn } from "@multica/ui/lib/utils";
import { ActorAvatar } from "../../../common/actor-avatar";
import { ReadonlyContent } from "../../../editor/readonly-content";
import { formatDuration } from "../../../agents/components/agent-activity-hover-content";
import { StepBody } from "../../../common/task-transcript/agent-transcript-dialog";
import {
  buildSteps,
  isCallStep,
  type TraceCallStep,
  type TraceRow,
} from "../../../common/task-transcript/build-steps";
import { buildTimeline, isOutputTruncated } from "../../../common/task-transcript/build-timeline";
import { traceEventSummary, traceToolArgSummary } from "../../../common/task-transcript/trace-event-presenter";
import { redactSecrets } from "../../../common/task-transcript/redact";
import { useT } from "../../../i18n";
import { commentRunOutput, isActiveCommentRun } from "../comment-runs";
import { useStatusLabel } from "../task-run-labels";

function toolKind(step: TraceCallStep): "run" | "list" | "read" | "write" | "call" {
  const input = step.call?.input;
  if (typeof input?.command === "string" || typeof input?.cmd === "string") return "run";
  if (typeof input?.pattern === "string" || typeof input?.glob === "string") return "list";
  if (typeof input?.old_string === "string" || typeof input?.content === "string") return "write";
  if (typeof input?.file_path === "string" || typeof input?.path === "string") return "read";
  return "call";
}

/**
 * Selected-run inspector: the real transcript, in the issue sidebar.
 * Collapsed rows are one line; open rows show the stored output, not a clip.
 */
export function RunInspector({
  task,
  onBack,
}: {
  task: AgentTask;
  onBack?: () => void;
}) {
  const { t } = useT("issues");
  const { getActorName } = useActorName();
  const agentName = getActorName("agent", task.agent_id);
  const statusLabel = useStatusLabel(task.status);
  const active = isActiveCommentRun(task);
  const [now, setNow] = useState(() => Date.now());
  const [taskOpen, setTaskOpen] = useState(false);
  const { data, isPending, isError, refetch } = useTaskMessages(task.id, active, true);
  const items = useMemo(() => buildTimeline(data ?? []), [data]);
  const rows = useMemo(() => buildSteps(items), [items]);
  const posted = commentRunOutput(task);
  const instruction = task.trigger_summary?.trim();
  const model = task.usage?.[0]?.model;

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);

  const start = task.started_at ?? task.dispatched_at;
  const duration =
    start && task.completed_at
      ? formatDuration(start, Date.parse(task.completed_at))
      : active
        ? formatDuration(task.started_at ?? task.dispatched_at ?? task.created_at, now)
        : "";

  return (
    <div data-testid="run-inspector">
      {onBack && (
        <button
          type="button"
          className="mb-2 flex items-center gap-1 text-caption text-muted-foreground hover:text-foreground"
          onClick={onBack}
        >
          <ChevronLeft className="size-3.5" />
          {t(($) => $.run_canvas.section)}
        </button>
      )}
      <header className="flex items-start gap-2">
        <ActorAvatar actorType="agent" actorId={task.agent_id} size="sm" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-body font-medium">{agentName}</p>
          {model && <p className="truncate text-micro text-muted-foreground">{model}</p>}
        </div>
        <span
          className={cn(
            "mt-0.5 shrink-0 rounded-full px-1.5 py-0.5 text-micro",
            task.status === "completed" && "bg-success/15 text-success",
            task.status === "failed" && "bg-destructive/15 text-destructive",
            isActiveCommentRun(task) && "bg-primary/15 text-primary",
            task.status !== "completed" &&
              task.status !== "failed" &&
              !isActiveCommentRun(task) &&
              "bg-muted text-muted-foreground",
          )}
        >
          {statusLabel}
        </span>
        {duration && <span className="mt-0.5 shrink-0 text-micro tabular-nums text-muted-foreground">{duration}</span>}
      </header>

      <div className="mt-3">
        {isPending ? (
          <p role="status" className="text-caption text-muted-foreground">
            {t(($) => $.inline_run.loading)}
          </p>
        ) : isError ? (
          <div role="alert" className="text-caption text-destructive">
            {t(($) => $.inline_run.load_failed)}
            <button className="ml-2 underline" type="button" onClick={() => void refetch()}>
              {t(($) => $.inline_run.try_again)}
            </button>
          </div>
        ) : (
          <>
            {instruction && (
              <section className="mb-4">
                <h3 className="mb-1.5 text-micro font-medium text-muted-foreground">
                  {t(($) => $.run_canvas.task)}
                </h3>
                <div className="rounded-lg bg-muted/40 px-3 py-2">
                  <div className={cn("text-caption break-words", !taskOpen && "max-h-24 overflow-hidden")}>
                    <ReadonlyContent content={redactSecrets(instruction)} />
                  </div>
                  <button
                    type="button"
                    className="mt-1.5 text-micro text-muted-foreground hover:text-foreground"
                    onClick={() => setTaskOpen(!taskOpen)}
                  >
                    {taskOpen ? t(($) => $.run_canvas.collapse) : t(($) => $.run_canvas.expand_all)}
                    <ChevronDown className={cn("ml-0.5 inline size-3", taskOpen && "rotate-180")} />
                  </button>
                </div>
              </section>
            )}

            <section>
              <div className="mb-1.5 flex items-baseline justify-between">
                <h3 className="text-micro font-medium text-muted-foreground">
                  {t(($) => $.run_canvas.process)}
                </h3>
                {rows.length > 0 && (
                  <span className="text-micro tabular-nums text-muted-foreground">
                    {t(($) => $.inline_run.steps, { count: rows.length })}
                  </span>
                )}
              </div>
              {rows.length === 0 && posted && (
                <div className="text-caption break-words">
                  <ReadonlyContent content={redactSecrets(posted)} />
                </div>
              )}
              {rows.length === 0 && !posted && !instruction && (
                <p className="text-caption text-muted-foreground">{t(($) => $.run_canvas.empty_result)}</p>
              )}
              <ol className="space-y-1">
                {rows.map((row) => (
                  <ProcessRow key={row.seq} row={row} />
                ))}
              </ol>
            </section>
          </>
        )}
      </div>
    </div>
  );
}

function ProcessRow({ row }: { row: TraceRow }) {
  const { t } = useT("issues");
  const [open, setOpen] = useState(false);

  if (isCallStep(row)) {
    const kind = toolKind(row);
    const Icon = kind === "run" ? Terminal : kind === "list" ? Search : kind === "write" ? SquarePen : FileText;
    const verb =
      kind === "run"
        ? t(($) => $.run_canvas.verb_run)
        : kind === "list"
          ? t(($) => $.run_canvas.verb_list)
          : kind === "write"
            ? t(($) => $.run_canvas.verb_write)
            : kind === "read"
              ? t(($) => $.run_canvas.verb_read)
              : t(($) => $.run_canvas.tool_call);
    const arg = redactSecrets(traceToolArgSummary(row.call?.input));
    const done = Boolean(row.result);
    const truncated = row.result ? isOutputTruncated(row.result) : false;
    const detail = row.result ?? row.call;
    return (
      <li>
        <button
          type="button"
          className="flex w-full items-start gap-2 rounded-md py-1 text-left hover:bg-accent/40"
          onClick={() => setOpen(!open)}
        >
          <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-border" aria-hidden="true" />
          <Icon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-caption">
            <span className="text-muted-foreground">{verb} </span>
            {arg}
          </span>
          <span className="mt-0.5 flex shrink-0 items-center gap-1">
            {truncated && (
              <span className="rounded-full bg-muted px-1.5 text-micro text-muted-foreground">
                {t(($) => $.run_canvas.truncated)}
              </span>
            )}
            {done && <span className="text-micro text-success">{t(($) => $.run_canvas.done)}</span>}
          </span>
        </button>
        {open && detail && (
          <div className="ml-6 mt-1 overflow-hidden rounded-md bg-muted/40">
            <StepBody item={detail} />
          </div>
        )}
      </li>
    );
  }

  if (row.kind === "thinking") {
    const full = redactSecrets(row.item.content ?? "");
    const preview = traceEventSummary({ type: "thinking", content: full });
    return (
      <li>
        <button
          type="button"
          className="flex w-full items-start gap-2 rounded-md py-1 text-left hover:bg-accent/40"
          onClick={() => setOpen(!open)}
        >
          <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-border" aria-hidden="true" />
          <Brain className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">
            <span className="text-foreground">{t(($) => $.run_canvas.thinking)} </span>
            {preview}
          </span>
        </button>
        {open && (
          <p className="ml-6 mt-1 whitespace-pre-wrap break-words text-caption text-muted-foreground">{full}</p>
        )}
      </li>
    );
  }

  if (row.kind === "error") {
    return (
      <li className="flex items-start gap-2 py-1">
        <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-destructive" aria-hidden="true" />
        <p role="alert" className="whitespace-pre-wrap break-words text-caption text-destructive">
          {row.item.content}
        </p>
      </li>
    );
  }

  const text = row.item.content?.trim();
  if (!text) return null;
  return (
    <li className="flex items-start gap-2 py-1">
      <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-border" aria-hidden="true" />
      <div className="min-w-0 flex-1 break-words text-caption">
        <ReadonlyContent content={redactSecrets(text)} />
      </div>
    </li>
  );
}
