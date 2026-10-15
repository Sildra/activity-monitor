import { Rpc } from "@opencode/plugin/rpc";

/**
 * RPC contract shared by the Activity Monitor server plugin (which knows the
 * connection state and the per-session task) and its TUI counterpart (which
 * renders them in the OpenCode sidebar). `status` and `task` answer on demand;
 * the `state` and `task` events are pushed on every change so the indicator
 * stays live.
 *
 * The state is one of `pending` (no request has completed yet), `ok` (the last
 * request succeeded), `error` (the last request failed) or `shadow` (the plugin
 * is running without reporting, i.e. `ACTIVITY_MONITOR_SHADOW` or a `/shadow`
 * turn). The task is the id a session pinned with `/task`, or `""` when it has
 * none.
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
        task: {
            input: {
                type: "object",
                properties: { sessionID: { type: "string" } },
                required: ["sessionID"],
                additionalProperties: false,
            },
            output: {
                type: "object",
                properties: { task: { type: "string" } },
                required: ["task"],
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
        task: {
            schema: {
                type: "object",
                properties: {
                    sessionID: { type: "string" },
                    task: { type: "string" },
                },
                required: ["sessionID", "task"],
                additionalProperties: false,
            },
        },
    },
});
