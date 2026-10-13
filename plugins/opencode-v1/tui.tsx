/** @jsxImportSource @opentui/solid */
import type { TuiPlugin, TuiPluginApi, TuiPluginModule } from "@opencode-ai/plugin/tui";
import { createSignal } from "solid-js";

type ConnectionState = "pending" | "ok" | "error" | "shadow";

const PREFIX = "activity-monitor:";

const parseState = (command: string): ConnectionState | undefined => {
    if (!command.startsWith(PREFIX)) {
        return undefined;
    }

    const state = command.slice(PREFIX.length);

    return state === "pending" || state === "ok" || state === "error" || state === "shadow" ? state : undefined;
};

const label = (state: ConnectionState): string => {
    if (state === "ok") {
        return "Connected";
    }

    if (state === "error") {
        return "Error";
    }

    if (state === "shadow") {
        return "Shadow";
    }

    return "Connecting";
};

function Indicator(props: { api: TuiPluginApi; state: ConnectionState }) {
    const theme = props.api.theme.current;
    const color = props.state === "ok" ? theme.success : props.state === "error" ? theme.error : theme.textMuted;

    return (
        <box flexDirection="row" gap={1}>
            <text flexShrink={0} fg={color}>
                {"\u2022"}
            </text>
            <text fg={theme.text}>
                <b>Activity Monitor</b>
            </text>
            <text fg={props.state === "error" ? theme.error : theme.textMuted}>{label(props.state)}</text>
        </box>
    );
}

/**
 * Renders the Activity Monitor connection state in the OpenCode TUI sidebar
 * (the right pane), just after the built-in MCP section: green when the last
 * report succeeded, red when it failed, gray while the plugin is shadowing
 * (`ACTIVITY_MONITOR_SHADOW`) or has not reported yet.
 *
 * V1 has no RPC bridge between a plugin's server and TUI halves, so the server
 * half (`opencode-activity.ts`) publishes the state as a namespaced
 * `tui.command.execute` event; this half subscribes to it. Load this file from
 * `tui.json` (see `plugins/README.md`). This is an OpenCode-only affordance:
 * nothing about it is sent to the Activity Monitor or shown in its web UI.
 */
const tui: TuiPlugin = async (api) => {
    const [state, setState] = createSignal<ConnectionState>("pending");

    api.event.on("tui.command.execute", (event) => {
        const next = parseState(event.properties.command);

        if (next !== undefined) {
            setState(next);
        }
    });

    api.slots.register({
        order: 210,
        slots: {
            sidebar_content() {
                return <Indicator api={api} state={state()} />;
            },
        },
    });
};

const plugin: TuiPluginModule & { id: string } = {
    id: "activity-monitor.tui",
    tui,
};

export default plugin;
