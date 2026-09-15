"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useActorName } from "@multica/core/workspace/hooks";
import type { AgentTask } from "@multica/core/types";
import { cn } from "@multica/ui/lib/utils";
import { ActorAvatar } from "../../../common/actor-avatar";
import { formatDuration } from "../../../agents/components/agent-activity-hover-content";
import { isActiveCommentRun } from "../comment-runs";
import { TaskStatusIcon } from "../task-status-icon";
import { useStatusLabel, useTriggerText } from "../task-run-labels";
import {
  connectorPaths,
  type DelegationForest,
  type DelegationNode,
  type NodeBox,
} from "./delegation-canvas";
import "./run-canvas.css";

type RegisterCard = (id: string, element: HTMLDivElement | null) => void;

type DrawnPath = { d: string; live: boolean };

function cardTone(task: AgentTask): "live" | "ok" | "bad" | "off" {
  if (isActiveCommentRun(task)) return "live";
  if (task.status === "completed") return "ok";
  if (task.status === "failed") return "bad";
  return "off";
}

/**
 * Shrink `children` to fit the viewport: never grow past natural size, never
 * introduce a scrollbar. `maxHeight` caps the inline strip; omit it to fill
 * the parent (the expanded dialog).
 *
 * Scale is a CSS transform so connector geometry stays in unscaled layout
 * pixels. Snap on resize — this view is on screen the whole time an issue is
 * open, so animating the fit would feel like the diagram is breathing.
 */
export function FitSurface({
  children,
  maxHeight,
  className,
}: {
  children: ReactNode;
  maxHeight?: number;
  className?: string;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ scale: 1, width: 0, height: 0 });

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!viewport || !content) return;

    const fit = () => {
      const vw = viewport.clientWidth;
      const vh = maxHeight ?? viewport.clientHeight;
      const cw = content.offsetWidth;
      const ch = content.offsetHeight;
      if (vw < 1 || vh < 1 || cw < 1 || ch < 1) return;
      const scale = Math.min(1, vw / cw, vh / ch);
      const width = Math.round(cw * scale);
      const height = Math.round(ch * scale);
      setBox((previous) =>
        previous.scale === scale && previous.width === width && previous.height === height
          ? previous
          : { scale, width, height },
      );
    };

    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(viewport);
    observer.observe(content);
    return () => observer.disconnect();
  }, [maxHeight, children]);

  return (
    <div
      ref={viewportRef}
      className={cn("relative w-full overflow-hidden", maxHeight == null && "h-full min-h-0", className)}
    >
      <div
        style={
          box.width > 0
            ? { width: box.width, height: box.height }
            : maxHeight != null
              ? { maxHeight }
              : { height: "100%" }
        }
      >
        <div
          ref={contentRef}
          className="w-max origin-top-left"
          style={{ transform: `scale(${box.scale})` }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

/**
 * Left-to-right delegation canvas: the run that started the chain on the left,
 * the runs it delegated to stacked beside it, one column per hop.
 *
 * The columns are laid out by the browser (`grid`), not by this component. Only
 * the connectors are computed here, from measured card boxes, which is what
 * lets a card grow to fit a long agent name or a wrapped trigger without the
 * geometry going stale — a fixed card height would have to be guessed, and it
 * would be wrong in every locale. A `ResizeObserver` on the container re-draws
 * when a card changes size, so a run finishing and swapping its live timer for
 * a duration moves its connectors with it.
 */
export function RunCanvas({
  forest,
  className,
  selectedId,
  onSelect,
}: {
  forest: DelegationForest;
  className?: string;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cards = useRef(new Map<string, HTMLDivElement>());
  const [drawing, setDrawing] = useState<{ width: number; height: number; paths: DrawnPath[] }>({
    width: 0,
    height: 0,
    paths: [],
  });

  const register = useCallback<RegisterCard>((id, element) => {
    if (element) cards.current.set(id, element);
    else cards.current.delete(id);
  }, []);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const draw = () => {
      const origin = container.getBoundingClientRect();
      // FitSurface scales this node; getBoundingClientRect is post-transform.
      // SVG lives in unscaled CSS pixels, so divide the screen delta back out.
      const factor = origin.width / Math.max(1, container.offsetWidth);
      const boxes = new Map<string, NodeBox>();
      for (const [id, element] of cards.current) {
        const rect = element.getBoundingClientRect();
        boxes.set(id, {
          left: (rect.left - origin.left) / factor,
          right: (rect.right - origin.left) / factor,
          centerY: (rect.top - origin.top + rect.height / 2) / factor,
        });
      }

      const paths: DrawnPath[] = [];
      const walk = (node: DelegationNode) => {
        const parent = boxes.get(node.task.id);
        const children = node.children
          .map((child) => boxes.get(child.task.id))
          .filter((box): box is NodeBox => box !== undefined);
        const live =
          isActiveCommentRun(node.task) ||
          node.children.some((child) => isActiveCommentRun(child.task));
        if (parent) paths.push(...connectorPaths(parent, children).map((d) => ({ d, live })));
        node.children.forEach(walk);
      };
      forest.roots.forEach(walk);

      const width = container.offsetWidth;
      const height = container.offsetHeight;
      setDrawing((previous) =>
        previous.width === width &&
        previous.height === height &&
        previous.paths.length === paths.length &&
        previous.paths.every((path, i) => path.d === paths[i]?.d && path.live === paths[i]?.live)
          ? previous
          : { width, height, paths },
      );
    };

    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(container);
    return () => observer.disconnect();
  }, [forest]);

  return (
    <div ref={containerRef} className={cn("relative w-max", className)}>
      <svg
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 overflow-visible"
        width={drawing.width}
        height={drawing.height}
      >
        {drawing.paths.map((path, index) => (
          <path
            key={`${path.d}-${index}`}
            d={path.d}
            fill="none"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            className={cn(
              "run-canvas-path",
              path.live ? "is-live stroke-primary/55" : "stroke-faint-foreground",
            )}
          />
        ))}
      </svg>
      <div className="flex flex-col items-start gap-4">
        {forest.roots.map((root) => (
          <Subtree key={root.task.id} node={root} register={register} selectedId={selectedId} onSelect={onSelect} />
        ))}
      </div>
    </div>
  );
}

/**
 * One run and, beside it, its delegated runs. `items-center` is what puts the
 * parent's centre line on the children block's centre, which `connectorPaths`
 * relies on when it starts the parent's stub.
 */
function Subtree({
  node,
  register,
  selectedId,
  onSelect,
}: {
  node: DelegationNode;
  register: RegisterCard;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
}) {
  const { task, children } = node;
  return (
    <div className="grid grid-cols-[auto_1fr] items-center gap-x-10">
      <div ref={(element) => register(task.id, element)} className="relative">
        <RunCard task={task} selected={selectedId === task.id} onSelect={onSelect} />
      </div>
      {children.length > 0 && (
        <div className="flex flex-col gap-3">
          {children.map((child) => (
            <Subtree
              key={child.task.id}
              node={child}
              register={register}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * `w-60` is fixed rather than `w-max` so every card occupies the same column
 * width: a per-card width would let the columns drift apart at depth, which is
 * the one thing the connector geometry assumes is not happening. Sized so three
 * hops fit the issue's reading column without scrolling.
 */
function RunCard({
  task,
  selected,
  onSelect,
}: {
  task: AgentTask;
  selected: boolean;
  onSelect?: (id: string) => void;
}) {
  const { getActorName } = useActorName();
  const agentName = getActorName("agent", task.agent_id);
  const statusLabel = useStatusLabel(task.status);
  const trigger = useTriggerText(task);
  const active = isActiveCommentRun(task);
  const tone = cardTone(task);
  const [now, setNow] = useState(() => Date.now());

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
  const model = task.usage?.[0]?.model;
  const detail = [model, duration].filter(Boolean).join(" · ");

  return (
    <div className="w-60">
      <button
        type="button"
        data-tone={tone}
        aria-pressed={selected}
        className="run-canvas-card block w-full cursor-pointer rounded-lg border border-border p-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => onSelect?.(task.id)}
      >
        <span className="flex w-full items-center gap-1.5">
          <span className={cn("shrink-0", active && "run-canvas-live")}>
            <ActorAvatar actorType="agent" actorId={task.agent_id} size="sm" />
          </span>
          <span className="min-w-0 flex-1 truncate text-body font-medium">{agentName}</span>
          <TaskStatusIcon status={task.status} />
          <span
            className={cn(
              "shrink-0 text-micro",
              tone === "live" && "text-primary",
              tone === "ok" && "text-success",
              tone === "bad" && "text-destructive",
              tone === "off" && "text-muted-foreground",
            )}
          >
            {statusLabel}
          </span>
        </span>

        {detail && (
          <span className="mt-1 block truncate text-micro tabular-nums text-muted-foreground">
            {detail}
          </span>
        )}
        {trigger && (
          <span className="mt-1 line-clamp-2 block text-caption text-muted-foreground">{trigger}</span>
        )}
      </button>
    </div>
  );
}
