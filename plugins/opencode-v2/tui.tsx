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

/**
 * Renders the Activity Monitor connection state in the TUI footer: green when
 * the last request succeeded, red when it failed, gray while the plugin is
 * running in shadow mode (or has not reported yet).
 *
 * The state lives in the server plugin, so it is read over the plugin RPC
 * contract in `rpc.ts` and kept in a hot-reload-safe in-memory store.
 */
export default Plugin.define({
    id: "activity-monitor.tui",
    async setup(context) {
        const [status, setStatus] = context.storage.memory<Status>("status", {
            initial: { state: "pending", message: "" },
        });

        const apply = (value: unknown): void => {
            const next = asStatus(value);

            setStatus((draft) => {
                draft.state = next.state;
                draft.message = next.message;
            });
        };

        const rpc = context.client.rpc(ActivityMonitor);
        const unsubscribe = rpc.events.on("state", (event) => apply(event.data));

        // The server plugin may have reported before this plugin subscribed, so
        // seed the indicator with the current value once (best effort).
        void rpc.status(undefined).then(apply, () => undefined);

        const render = () => {
            const text = context.theme.text;
            const color =
                status.state === "ok"
                    ? text.feedback.success.base
                    : status.state === "error"
                      ? text.feedback.error.base
                      : text.muted;
            const label = status.state === "error" ? "activity-monitor: error" : "activity-monitor";

            return <text fg={color}>{`\u25cf ${label}`}</text>;
        };

        const unregisterHome = context.ui.slot({ append: "home.footer.status", render });
        const unregisterPrompt = context.ui.slot({ append: "prompt.footer.status", render });

        return () => {
            unsubscribe();
            unregisterHome();
            unregisterPrompt();
        };
    },
});
