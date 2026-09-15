// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentTask } from "@multica/core/types";
import { renderWithI18n } from "../../../test/i18n";

const mockState = vi.hoisted(() => ({
  tasks: [] as AgentTask[],
}));

vi.mock("@multica/core/workspace/hooks", () => ({
  useActorName: () => ({
    getActorName: (_type: string, id: string) => `agent ${id}`,
  }),
}));

// The avatar drags in hover cards, presence and navigation; none of it is what
// this suite is about, and all of it would need providers.
vi.mock("../../../common/actor-avatar", () => ({
  ActorAvatar: ({ actorId }: { actorId: string }) => (
    <span data-testid={`avatar-${actorId}`} />
  ),
}));

// Every card reads its own transcript. The read itself is covered by core's
// suite; here the chain only has to hand the result to the dialog.
vi.mock("@multica/core/chat/queries", () => ({
  useTaskMessages: () => ({
    data: [{ seq: 1, type: "thinking", content: "x" }],
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));


vi.mock("@tanstack/react-query", async () => {
  const actual =
    await vi.importActual<typeof import("@tanstack/react-query")>(
      "@tanstack/react-query",
    );

  return {
    ...actual,
    useQuery: (opts: { queryKey?: readonly unknown[] }) => {
      // Per-issue run list: issueKeys.tasks(issueId) === ["issues", "tasks", id]
      if (opts.queryKey?.[0] === "issues" && opts.queryKey?.[1] === "tasks") {
        return { data: mockState.tasks };
      }
      return actual.useQuery(opts as Parameters<typeof actual.useQuery>[0]);
    },
  };
});

import { RunChainSection } from "./run-canvas-section";

const run = (
  id: string,
  delegatedFrom?: string,
  createdAt = "2026-06-08T08:00:00Z",
  status: AgentTask["status"] = "completed",
): AgentTask => ({
  id,
  agent_id: `agent-${id}`,
  runtime_id: "runtime-1",
  issue_id: "issue-1",
  status,
  priority: 0,
  dispatched_at: null,
  started_at: "2026-06-08T08:00:00Z",
  completed_at: status === "completed" ? "2026-06-08T08:01:00Z" : null,
  result: null,
  error: null,
  created_at: createdAt,
  attribution: delegatedFrom
    ? { source: "delegation", precise: true, delegated_from_task_id: delegatedFrom }
    : undefined,
});

const CHAIN = [
  run("leader"),
  run("explorer", "leader", "2026-06-08T08:00:01Z"),
  run("runner", "leader", "2026-06-08T08:00:02Z"),
];

/** Cards carry the agent name as their accessible name, in tree order. */
const cards = () => screen.queryAllByTestId(/^avatar-/);

function renderChain(locale?: "en" | "zh-Hans", issueStatus?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithI18n(
    <QueryClientProvider client={client}>
      <RunChainSection issueId="issue-1" issueStatus={issueStatus} />
    </QueryClientProvider>,
    { locale },
  );
}

beforeEach(() => {
  mockState.tasks = [];
});

describe("run chain section", () => {
  it("stays hidden while the issue has delegated nothing", () => {
    // Two unrelated runs. One connectorless card would read as a broken chain.
    mockState.tasks = [run("a"), run("b")];

    renderChain();

    expect(screen.queryByRole("button", { name: /Run chain/ })).not.toBeInTheDocument();
  });

  it("stays hidden for an empty issue", () => {
    renderChain();

    expect(screen.queryByRole("button", { name: /Run chain/ })).not.toBeInTheDocument();
  });

  it("draws the canvas without any interaction", () => {
    // The point of moving off the sidebar: the diagram has to be on screen when
    // the issue opens, because nobody clicks a thing they do not know exists.
    mockState.tasks = CHAIN;

    renderChain();

    expect(cards().map((el) => el.dataset.testid)).toEqual([
      "avatar-agent-leader",
      "avatar-agent-explorer",
      "avatar-agent-runner",
    ]);
  });

  it("counts every run in the chain, not just the roots", () => {
    mockState.tasks = CHAIN;

    renderChain();

    expect(screen.getByRole("button", { name: /3 runs/ })).toBeInTheDocument();
  });

  it("names the section in the reader's locale", () => {
    mockState.tasks = [run("leader"), run("worker", "leader", "2026-06-08T08:00:01Z")];

    renderChain("zh-Hans");

    expect(screen.getByRole("button", { name: /运行链路/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /2 个运行/ })).toBeInTheDocument();
  });

  it("folds and unfolds the canvas from its header", () => {
    mockState.tasks = CHAIN;
    renderChain();

    const header = screen.getByRole("button", { name: /Run chain/ });
    expect(header).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(header);
    expect(cards()).toHaveLength(0);
    expect(header).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(header);
    expect(cards()).toHaveLength(3);
  });

  it("marks a chain that still has a run in flight", () => {
    mockState.tasks = [run("leader"), run("worker", "leader", "2026-06-08T08:00:01Z", "running")];

    renderChain();

    expect(screen.getByRole("button", { name: /1 running/ })).toBeInTheDocument();
  });

  it("names a finished chain the issue has not accepted yet", () => {
    mockState.tasks = CHAIN;

    renderChain("en", "in_progress");

    expect(screen.getByRole("button", { name: /waiting for review/ })).toBeInTheDocument();
  });

  it("does not call a live chain waiting for review", () => {
    mockState.tasks = [run("leader"), run("worker", "leader", "2026-06-08T08:00:01Z", "running")];

    renderChain("en", "in_progress");

    expect(screen.getByRole("button", { name: /Run chain/ })).not.toHaveAccessibleName(/waiting/);
  });

  it("counts only the runs on the chain when deciding whether it is live", () => {
    // An unrelated run finishing elsewhere in the issue must not make this
    // header look live.
    mockState.tasks = [
      ...CHAIN,
      run("unrelated", undefined, "2026-06-08T08:00:03Z", "running"),
    ];

    renderChain();

    expect(screen.getByRole("button", { name: /Run chain/ })).not.toHaveAccessibleName(/running/);
  });

  it("leaves a run that delegated nothing off the canvas", () => {
    mockState.tasks = [run("lonely"), run("leader"), run("worker", "leader", "2026-06-08T08:00:01Z")];

    renderChain();

    expect(cards()).toHaveLength(2);
  });

  it("marks the card that was clicked as selected", () => {
    mockState.tasks = CHAIN;
    renderChain();

    const explorer = screen.getAllByRole("button", { name: /Completed/ })[1]!;
    fireEvent.click(explorer);

    expect(explorer).toHaveAttribute("aria-pressed", "true");
  });

  it("selects from anywhere on the card, not just its first row", () => {
    mockState.tasks = [run("leader"), run("worker", "leader", "2026-06-08T08:00:01Z")];
    renderChain();

    const card = screen.getByTestId("avatar-agent-worker").closest("button");
    expect(card).not.toBeNull();
    fireEvent.click(card as HTMLElement);

    expect(card).toHaveAttribute("aria-pressed", "true");
  });

  it("opens an enlarged canvas from the expand control", () => {
    mockState.tasks = CHAIN;
    renderChain();

    fireEvent.click(screen.getByRole("button", { name: /Expand canvas/ }));

    expect(screen.getByRole("dialog", { name: /Run chain/ })).toBeInTheDocument();
    expect(cards()).toHaveLength(3);
  });
});
