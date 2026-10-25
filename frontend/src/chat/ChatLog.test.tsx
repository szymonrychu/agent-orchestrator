// @vitest-environment jsdom
import { ThemeProvider } from "@mui/material/styles";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { AgentView, ClusterView, EventView } from "../api/client";
import type { StreamOptions } from "../api/stream";
import { theme } from "../theme";
import { ChatView } from "./ChatView";
import { FOLLOW_PX, nextFirstIndex, START_INDEX } from "./ChatLog";
import type { ChatItem } from "./chatModel";

const virtuosoProps = vi.hoisted(() => ({
  last: null as null | {
    firstItemIndex: number;
    followOutput: (b: boolean) => unknown;
    atBottomThreshold: number;
  },
}));
const api = vi.hoisted(() => ({ listEvents: vi.fn(), listMessages: vi.fn(async () => []) }));
const stream = vi.hoisted(() => ({
  opened: [] as { url: string; opts: StreamOptions }[],
  close: vi.fn(),
}));

vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  ...api,
  eventStreamUrl: (id: string) => `/api/v1/clusters/${id}/events/stream`,
}));
vi.mock("../api/stream", () => ({
  openStream: (url: string, opts: StreamOptions) => {
    // Record only the chat event stream; the queue strip's cluster status stream is covered in Composer.test.tsx.
    if (!url.includes("/events/")) return { close: vi.fn() };
    stream.opened.push({ url, opts });
    return { close: stream.close };
  },
}));
// jsdom has no layout, so Virtuoso renders nothing: a stand-in that renders every item, the header and a start-reached trigger.
vi.mock("react-virtuoso", () => ({
  Virtuoso: (props: {
    data: unknown[];
    firstItemIndex: number;
    itemContent: (i: number, d: unknown) => React.ReactNode;
    startReached: () => void;
    followOutput: (atBottom: boolean) => unknown;
    atBottomThreshold: number;
    components: { Header: () => React.ReactNode };
  }) => {
    virtuosoProps.last = props;
    return (
      <div>
        <props.components.Header />
        <button onClick={props.startReached}>reach top</button>
        {props.data.map((d, i) => (
          <div key={props.firstItemIndex + i}>
            {props.itemContent(props.firstItemIndex + i, d)}
          </div>
        ))}
      </div>
    );
  },
}));

const cluster = {
  id: "c1",
  slug: "alpha",
  desired: "active",
  summary: { state: "working" },
  spec: { replicas: 1 },
  max_replicas: 1,
  static: false,
} as unknown as ClusterView;
const agent = (id: string, ordinal: number): AgentView =>
  ({
    id,
    ordinal,
    state: "idle",
    ready: true,
    gone: false,
    pod_name: `alpha-${ordinal}`,
  }) as unknown as AgentView;
const agents = [agent("a0", 0), agent("a1", 1)];

const ev = (
  id: number,
  type: string,
  data: Record<string, unknown>,
  agent_id = "a0",
): EventView =>
  ({
    id,
    seq: id,
    type,
    data,
    agent_id,
    cluster_id: "c1",
    session_id: "s",
    ts: "2026-01-01T00:00:00Z",
    turn_id: "t1",
  }) as EventView;
const text = (id: number, t: string, agent_id = "a0") =>
  ev(id, "assistant_text", { text: t }, agent_id);

function mount(c: ClusterView = cluster, maxItems?: number) {
  return render(
    <ThemeProvider theme={theme}>
      <ChatView cluster={c} agents={agents} maxItems={maxItems} />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  stream.opened.length = 0;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ChatLog", () => {
  test("loads newest page on open", async () => {
    api.listEvents.mockResolvedValue({
      events: [text(5, "hello five"), text(6, "hello six")],
      has_more_before: true,
      next_after_id: 6,
    });
    mount();
    expect(await screen.findByText("hello six")).toBeTruthy();
    expect(screen.getByText("hello five")).toBeTruthy();
    expect(api.listEvents).toHaveBeenCalledWith("c1", { limit: 200 });
    expect(stream.opened[0].url).toBe("/api/v1/clusters/c1/events/stream");
    expect(stream.opened[0].opts.afterId).toBe(6);
    expect(screen.getByText("Loading older messages")).toBeTruthy();
  });

  test("loads older page on start reached", async () => {
    api.listEvents.mockResolvedValueOnce({
      events: [text(5, "newer")],
      has_more_before: true,
      next_after_id: 5,
    });
    mount();
    await screen.findByText("newer");
    api.listEvents.mockResolvedValueOnce({
      events: [text(3, "older")],
      has_more_before: false,
      next_after_id: 3,
    });
    fireEvent.click(screen.getByText("reach top"));
    expect(await screen.findByText("older")).toBeTruthy();
    expect(api.listEvents).toHaveBeenLastCalledWith("c1", {
      limit: 200,
      before_id: 5,
    });
    expect(await screen.findByText("Start of history")).toBeTruthy();
    fireEvent.click(screen.getByText("reach top"));
    expect(api.listEvents).toHaveBeenCalledTimes(2);
  });

  test("appends live events", async () => {
    api.listEvents.mockResolvedValue({
      events: [text(5, "first")],
      has_more_before: false,
      next_after_id: 5,
    });
    mount();
    await screen.findByText("first");
    act(() =>
      stream.opened[0].opts.onEvent(text(6, "live one"), {
        type: "assistant_text",
        id: "6",
      }),
    );
    expect(screen.getByText("live one")).toBeTruthy();
    act(() =>
      stream.opened[0].opts.onEvent(text(6, "live one"), {
        type: "assistant_text",
        id: "6",
      }),
    );
    expect(screen.getAllByText("live one")).toHaveLength(1);
  });

  test("drops stale responses after switch", async () => {
    let resolveOld: (v: unknown) => void = () => {};
    api.listEvents.mockImplementationOnce(
      () => new Promise((r) => (resolveOld = r)),
    );
    const other = { ...cluster, id: "c2", slug: "beta" } as ClusterView;
    const view = mount();
    api.listEvents.mockResolvedValueOnce({
      events: [text(9, "from beta")],
      has_more_before: false,
      next_after_id: 9,
    });
    view.rerender(
      <ThemeProvider theme={theme}>
        <ChatView cluster={other} agents={agents} />
      </ThemeProvider>,
    );
    await screen.findByText("from beta");
    await act(async () =>
      resolveOld({
        events: [text(1, "from alpha")],
        has_more_before: false,
        next_after_id: 1,
      }),
    );
    expect(screen.queryByText("from alpha")).toBeNull();
    expect(screen.getByText("from beta")).toBeTruthy();
    expect(stream.opened.at(-1)?.url).toBe("/api/v1/clusters/c2/events/stream");
  });

  test("expands tool and thinking entries", async () => {
    api.listEvents.mockResolvedValue({
      events: [
        ev(1, "thinking", { text: "pondering deeply" }),
        ev(2, "tool_call", {
          tool_use_id: "u1",
          tool_name: "Bash",
          tool_input: { cmd: "ls" },
        }),
        ev(3, "tool_result", {
          tool_use_id: "u1",
          response: "file-a file-b",
          is_error: false,
        }),
      ],
      has_more_before: false,
      next_after_id: 3,
    });
    mount();
    await screen.findByText("Bash");
    expect(screen.queryByText("pondering deeply")).toBeNull();
    expect(screen.queryByText("file-a file-b")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /thinking/i }));
    expect(screen.getByText("pondering deeply")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Bash/ }));
    expect(screen.getByText("file-a file-b")).toBeTruthy();
    expect(screen.getByText(/"cmd": "ls"/)).toBeTruthy();
  });

  test("filters by agent", async () => {
    api.listEvents.mockResolvedValue({
      events: [
        text(1, "from zero", "a0"),
        text(2, "from one", "a1"),
        ev(3, "user_message", { text: "hi there", origin: "user" }, "a0"),
      ],
      has_more_before: false,
      next_after_id: 3,
    });
    mount();
    await screen.findByText("from one");
    const chips = screen.getByRole("group", { name: "agent filter" });
    fireEvent.click(within(chips).getByRole("button", { name: "agent 1" }));
    expect(screen.queryByText("from zero")).toBeNull();
    expect(screen.getByText("from one")).toBeTruthy();
    fireEvent.click(within(chips).getByRole("button", { name: "agent 1" }));
    await waitFor(() => expect(screen.getByText("from zero")).toBeTruthy());
    expect(screen.getAllByLabelText("agent 1 row").length).toBeGreaterThan(0);
  });

  test("trims the live window and pages from the new oldest id", async () => {
    const rows = Array.from({ length: 20 }, (_, i) =>
      text(i + 1, `e${i + 1}`),
    );
    api.listEvents.mockResolvedValue({
      events: rows,
      has_more_before: false,
      next_after_id: 20,
    });
    mount(cluster, 20);
    await screen.findByText("e20");
    expect(screen.getByText("Start of history")).toBeTruthy();
    const start = virtuosoProps.last!.firstItemIndex;
    act(() =>
      stream.opened[0].opts.onEvent(text(21, "e21"), {
        type: "assistant_text",
        id: "21",
      }),
    );
    act(() =>
      stream.opened[0].opts.onEvent(text(22, "e22"), {
        type: "assistant_text",
        id: "22",
      }),
    );
    expect(screen.queryByText("e1")).toBeNull();
    expect(screen.queryByText("e2")).toBeNull();
    expect(screen.getByText("e3")).toBeTruthy();
    expect(screen.getByText("Loading older messages")).toBeTruthy();
    expect(virtuosoProps.last!.firstItemIndex).toBe(start + 2);
    api.listEvents.mockResolvedValue({
      events: [],
      has_more_before: false,
      next_after_id: null,
    });
    fireEvent.click(screen.getByText("reach top"));
    await waitFor(() =>
      expect(api.listEvents).toHaveBeenLastCalledWith("c1", {
        limit: 200,
        before_id: 3,
      }),
    );
  });

  test("sticks to the bottom within 48 px only", async () => {
    api.listEvents.mockResolvedValue({
      events: [text(5, "x")],
      has_more_before: false,
      next_after_id: 5,
    });
    mount();
    await screen.findByText("x");
    expect(virtuosoProps.last!.atBottomThreshold).toBe(48);
    expect(FOLLOW_PX).toBe(48);
    expect(virtuosoProps.last!.followOutput(true)).toBe("auto");
    expect(virtuosoProps.last!.followOutput(false)).toBe(false);
  });
});

describe("nextFirstIndex", () => {
  const item = (eventId: number) => ({ eventId }) as unknown as ChatItem;
  const list = (...ids: number[]) => ids.map(item);
  test("prepending older items lowers the index by the count added", () => {
    expect(
      nextFirstIndex(
        { items: list(5, 6), index: START_INDEX },
        list(3, 4, 5, 6),
      ),
    ).toBe(START_INDEX - 2);
  });
  test("trimming the front raises the index by the count removed", () => {
    expect(
      nextFirstIndex({ items: list(1, 2, 3), index: START_INDEX }, list(3, 4)),
    ).toBe(START_INDEX + 2);
  });
  test("appending keeps the index", () => {
    expect(
      nextFirstIndex({ items: list(1, 2), index: START_INDEX }, list(1, 2, 3)),
    ).toBe(START_INDEX);
  });
  test("empty lists keep the index", () => {
    expect(nextFirstIndex({ items: [], index: 7 }, list(1))).toBe(7);
  });
});
