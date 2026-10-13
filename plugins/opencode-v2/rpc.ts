import { Rpc } from "@opencode/plugin/rpc";

/**
 * RPC contract shared by the Activity Monitor server plugin (which knows the
 * connection state) and its TUI counterpart (which renders it in the OpenCode
 * footer). `status` answers on demand; the `state` event is pushed on every
 * change so the indicator stays live.
 *
 * The state is one of `pending` (no request has completed yet), `ok` (the last
 * request succeeded), `error` (the last request failed) or `shadow` (the plugin
 * is running without reporting, i.e. `ACTIVITY_MONITOR_SHADOW` or a `/shadow`
 * turn).
 */
export const ActivityMonitor = Rpc.define({
    id: "activity-monitor",
    methods: {
        status: {
            input: { type: "object", properties: {}, additionalProperties: false },
            output: {
                type: "object",
                properties: {
                    state: { type: "string", enum: ["pending", "ok", "error", "shadow"] },
                    message: { type: "string" },
                },
                required: ["state", "message"],
                additionalProperties: false,
            },
        },
    },
    events: {
        state: {
            schema: {
                type: "object",
                properties: {
                    state: { type: "string", enum: ["pending", "ok", "error", "shadow"] },
                    message: { type: "string" },
                },
                required: ["state", "message"],
                additionalProperties: false,
            },
        },
    },
});
