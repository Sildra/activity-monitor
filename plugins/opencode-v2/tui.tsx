import { Plugin } from "@opencode/plugin/tui";
import { ActivityMonitor } from "./rpc.js";

type Status = { state: string; message: string };

const asStatus = (value: unknown): Status => {
    const data = (value ?? {}) as Record<string, unknown>;

    return {
        state: typeof data.state === "string" ? data.state : "pending",
        message: typeof data.message === "string" ? data.message : "",
    };
};

const asTask = (value: unknown): string => {
    const data = (value ?? {}) as Record<string, unknown>;

    return typeof data.task === "string" ? data.task : "";
};

/**
 * Renders the Activity Monitor connection state, and the task the current
 * session pinned with `/task`, in the TUI sidebar (the right pane), directly
 * below the built-in MCP and Context sections: green when the last request
 * succeeded, red when it failed, gray while the plugin is running in shadow mode
 * (or has not reported yet).
 *
 * The line reads `● Activity Monitor (<task>) <state>`; an unset task is left
 * out. Both values live in the server plugin, so they are read over the plugin
 * RPC contract in `rpc.ts` and kept in hot-reload-safe in-memory stores.
 */
export default Plugin.define({
    id: "activity-monitor.tui",
    async setup(context) {
        const [status, setStatus] = context.storage.memory<Status>("status", {
            initial: { state: "pending", message: "" },
        });
        const [tasks, setTasks] = context.storage.memory<{ bySession: Record<string, string> }>("tasks", {
            initial: { bySession: {} },
        });

        const apply = (value: unknown): void => {
            const next = asStatus(value);

            setStatus((draft) => {
                draft.state = next.state;
                draft.message = next.message;
            });
        };

        const remember = (sessionID: string, task: string): void => {
            setTasks((draft) => {
                draft.bySession = { ...draft.bySession, [sessionID]: task };
            });
        };

        const rpc = context.client.rpc(ActivityMonitor);
        const unsubscribe = rpc.events.on("state", (event) => apply(event.data));
        const unsubscribeTask = rpc.events.on("task", (event) => {
            const data = (event.data ?? {}) as Record<string, unknown>;

            if (typeof data.sessionID === "string") {
                remember(data.sessionID, typeof data.task === "string" ? data.task : "");
            }
        });

        // The server plugin may have reported before this plugin subscribed, so
        // seed the current values once (best effort). The RPC domain is registered
        // for the location its server half was loaded in, so the calls have to
        // name that location (`rpc.unavailable` otherwise) and carry the method's
        // required input.
        const location = context.location ?? context.data.location.default();
        void rpc.status({}, { location }).then(apply, () => undefined);

        // A session's task is only known to the server plugin, so the first time
        // the sidebar renders a session, ask for it (once per session).
        const requested = new Set<string>();

        const ensureTask = (sessionID: string): void => {
            if (sessionID === "" || requested.has(sessionID)) {
                return;
            }

            requested.add(sessionID);

            void rpc.task({ sessionID }, { location }).then(
                (value) => remember(sessionID, asTask(value)),
                () => requested.delete(sessionID),
            );
        };

        const render = (input: { sessionID: string }) => {
            const theme = context.theme;
            const color =
                status.state === "ok"
                    ? theme.text.feedback.success.base
                    : status.state === "error"
                      ? theme.text.feedback.error.base
                      : theme.text.muted;
            const label =
                status.state === "ok"
                    ? "Connected"
                    : status.state === "error"
                      ? "Error"
                      : status.state === "shadow"
                        ? "Shadow"
                        : "Connecting";
            const task = tasks.bySession[input.sessionID] ?? "";

            ensureTask(input.sessionID);

            return (
                <box flexDirection="row" gap={1} minWidth={0}>
                    <text flexShrink={0} fg={color}>
                        {"\u2022"}
                    </text>
                    <text flexShrink={0} fg={theme.text.base}>
                        <b>Activity Monitor</b>
                    </text>
                    {task === "" ? null : (
                        <text wrapMode="none" truncate flexShrink={1} minWidth={0} fg={theme.text.muted}>
                            {`(${task})`}
                        </text>
                    )}
                    <text
                        wrapMode="none"
                        truncate
                        flexGrow={1}
                        flexShrink={1}
                        minWidth={0}
                        fg={status.state === "error" ? theme.text.feedback.error.base : theme.text.muted}
                    >
                        {label}
                    </text>
                </box>
            );
        };

        const unregisterSidebar = context.ui.slot({ append: "sidebar.content", render });

        return () => {
            unsubscribe();
            unsubscribeTask();
            unregisterSidebar();
        };
    },
});
