// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { AgentTask } from "@multica/core/types";
import {
  buildDelegationForest,
  connectorPaths,
  type NodeBox,
} from "./delegation-canvas";

const baseTask: AgentTask = {
  id: "run-0",
  agent_id: "agent-0",
  runtime_id: "runtime-1",
  issue_id: "issue-1",
  status: "completed",
  priority: 0,
  dispatched_at: null,
  started_at: "2026-06-08T08:00:00Z",
  completed_at: "2026-06-08T08:01:00Z",
  result: null,
  error: null,
  created_at: "2026-06-08T08:00:00Z",
};

/** `delegatedFrom` is the delegation edge; absent means "this run has no parent". */
const run = (
  id: string,
  delegatedFrom?: string,
  createdAt = "2026-06-08T08:00:00Z",
  extra: Partial<AgentTask> = {},
): AgentTask => ({
  ...baseTask,
  id,
  agent_id: `agent-${id}`,
  created_at: createdAt,
  attribution: delegatedFrom
    ? { source: "delegation", precise: true, delegated_from_task_id: delegatedFrom }
    : undefined,
  ...extra,
});

/** The tree as `id(child) <- parent` pairs, so assertions read like the graph. */
const shape = (roots: ReturnType<typeof buildDelegationForest>["roots"]): string[] => {
  const out: string[] = [];
  const walk = (node: (typeof roots)[number], parent: string | null) => {
    out.push(`${node.task.id}<-${parent ?? "root"}`);
    node.children.forEach((child) => walk(child, node.task.id));
  };
  roots.forEach((root) => walk(root, null));
  return out;
};

describe("delegation forest", () => {
  it("hangs an @-mentioned agent's run under the run that mentioned it", () => {
    const forest = buildDelegationForest([
      run("leader"),
      run("explorer", "leader", "2026-06-08T08:00:01Z"),
      run("test-runner", "leader", "2026-06-08T08:00:02Z"),
    ]);

    expect(shape(forest.roots)).toEqual([
      "leader<-root",
      "explorer<-leader",
      "test-runner<-leader",
    ]);
    expect(forest.runCount).toBe(3);
    expect(forest.edgeCount).toBe(2);
  });

  it("keeps runs that touch no edge off the canvas", () => {
    // Three unrelated runs on one issue are the execution log's story. Drawing
    // them here would put three connectorless cards where a chain should be.
    const forest = buildDelegationForest([run("a"), run("b"), run("c")]);

    expect(forest.roots).toEqual([]);
    expect(forest.runCount).toBe(0);
  });

  it("keeps an isolated run out while still drawing the chain beside it", () => {
    const forest = buildDelegationForest([
      run("lonely"),
      run("leader"),
      run("worker", "leader", "2026-06-08T08:00:01Z"),
    ]);

    expect(shape(forest.roots)).toEqual(["leader<-root", "worker<-leader"]);
  });

  it("does not treat an auto-retry of the same agent as a delegation", () => {
    // `parent_task_id` is the same agent trying again, not a hand-off.
    const forest = buildDelegationForest([
      run("leader"),
      run("worker", "leader", "2026-06-08T08:00:01Z"),
      run("worker-retry", undefined, "2026-06-08T08:00:02Z", { parent_task_id: "worker" }),
    ]);

    expect(shape(forest.roots)).toEqual(["leader<-root", "worker<-leader"]);
  });

  it("orders siblings by creation, not by array order", () => {
    const forest = buildDelegationForest([
      run("second", "leader", "2026-06-08T08:00:02Z"),
      run("leader"),
      run("first", "leader", "2026-06-08T08:00:01Z"),
    ]);

    expect(shape(forest.roots)).toEqual(["leader<-root", "first<-leader", "second<-leader"]);
  });

  it("drops an edge whose parent is not in the given runs", () => {
    const forest = buildDelegationForest([run("worker", "run-in-another-issue")]);

    expect(forest.roots).toEqual([]);
  });

  it("breaks a loop instead of hanging, surfacing both loop members as roots", () => {
    // Impossible from the API (a run only points at an older one) but reachable
    // by a hand-edited row, and an unguarded walk would never return. Every
    // member of the cycle loses its own edge, which is what proves the forest
    // is acyclic: a cycle containing `task` would always be caught by `task`.
    const forest = buildDelegationForest([run("a", "b"), run("b", "a")]);

    expect(shape(forest.roots)).toEqual([]);
    expect(forest.edgeCount).toBe(0);
  });

  it("keeps a real edge that merely passes through a loop it is not part of", () => {
    // Regression: the first guard asked "is there a loop anywhere up the
    // chain", so `leaf -> a` was dropped just because `a` sits in one. That
    // deleted a genuine hand-off and left a cardless tree.
    const forest = buildDelegationForest([
      run("a", "b"),
      run("b", "a"),
      run("leaf", "a", "2026-06-08T08:00:01Z"),
    ]);

    expect(shape(forest.roots)).toEqual(["a<-root", "leaf<-a"]);
  });

  it("keeps depth beyond one level", () => {
    const forest = buildDelegationForest([
      run("leader"),
      run("mid", "leader", "2026-06-08T08:00:01Z"),
      run("deep", "mid", "2026-06-08T08:00:02Z"),
    ]);

    expect(shape(forest.roots)).toEqual(["leader<-root", "mid<-leader", "deep<-mid"]);
  });
});

const box = (left: number, right: number, centerY: number): NodeBox => ({
  left,
  right,
  centerY,
});

describe("connector paths", () => {
  it("draws nothing when the card delegated to nobody", () => {
    expect(connectorPaths(box(0, 100, 50), [])).toEqual([]);
  });

  it("joins a single child with one straight line", () => {
    const paths = connectorPaths(box(0, 100, 50), [box(124, 224, 50)]);

    // Parent right edge -> spine (112) -> child left edge, all on one row.
    expect(paths).toEqual(["M 100 50 H 112", "M 112 50 H 124"]);
  });

  it("rounds the spine into the outer children and leaves a bare stub for the rest", () => {
    const paths = connectorPaths(box(0, 100, 100), [
      box(124, 224, 40),
      box(124, 224, 100),
      box(124, 224, 160),
    ]);

    // Parent stub, then ONE spine whose two ends turn into the outermost
    // children, then a plain stub for the middle one.
    expect(paths).toHaveLength(3);
    expect(paths[0]).toBe("M 100 100 H 112");
    expect(paths[1]).toBe(
      "M 124 40 H 120 Q 112 40 112 48 V 152 Q 112 160 120 160 H 124",
    );
    expect(paths[2]).toBe("M 112 100 H 124");
  });

  it("never rounds below the first or last child's centre line", () => {
    // Two children 6px apart cannot absorb the 8px radius; clamping it is what
    // keeps the spine from doubling back on itself.
    const paths = connectorPaths(box(0, 100, 100), [box(124, 224, 100), box(124, 224, 106)]);

    expect(paths[1]).toBe(
      "M 124 100 H 115 Q 112 100 112 103 V 103 Q 112 106 115 106 H 124",
    );
  });

  it("rounds the child end to nothing when there is no gap to cross", () => {
    const paths = connectorPaths(box(0, 100, 100), [box(100, 200, 40), box(100, 200, 160)]);

    // spineX sits on the parent's edge, so the radius collapses and the spine
    // becomes two square corners rather than an overlapping curve.
    expect(paths[1]).toBe("M 100 40 H 100 Q 100 40 100 40 V 160 Q 100 160 100 160 H 100");
  });
});
