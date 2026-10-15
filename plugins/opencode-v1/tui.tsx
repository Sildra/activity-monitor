/** @jsxImportSource @opentui/solid */
import type { TuiPlugin, TuiPluginApi, TuiPluginModule } from "@opencode-ai/plugin/tui";
import { createSignal } from "solid-js";

type ConnectionState = "pending" | "ok" | "error" | "shadow";

const PREFIX = "activity-monitor:";
const TASK_PREFIX = `${PREFIX}task:`;

const parseState = (command: string): ConnectionState | undefined => {
    if (!command.startsWith(PREFIX)) {
        return undefined;
    }

    const state = command.slice(PREFIX.length);

    return state === "pending" || state === "ok" || state === "error" || state === "shadow" ? state : undefined;
};

// The server half forwards a session's `/task` id as
// `activity-monitor:task:<sessionID>:<task>`; the task is user text and may
// itself contain ":", so only the first separator counts.
const parseTask = (command: string): { sessionID: string; task: string } | undefined => {
    if (!command.startsWith(TASK_PREFIX)) {
        return undefined;
    }

    const rest = command.slice(TASK_PREFIX.length);
    const separator = rest.indexOf(":");

    return separator < 0 ? undefined : { sessionID: rest.slice(0, separator), task: rest.slice(separator + 1) };
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

function Indicator(props: { api: TuiPluginApi; state: ConnectionState; task: string }) {
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
            {props.task === "" ? null : <text fg={theme.textMuted}>{`(${props.task})`}</text>}
            <text fg={props.state === "error" ? theme.error : theme.textMuted}>{label(props.state)}</text>
        </box>
    );
}

/**
 * Renders the Activity Monitor connection state, and the task the open session
 * pinned with `/task`, in the OpenCode TUI sidebar (the right pane), just after
 * the built-in MCP section: green when the last report succeeded, red when it
 * failed, gray while the plugin is shadowing (`ACTIVITY_MONITOR_SHADOW`) or has
 * not reported yet. The line reads
 * `● Activity Monitor (<task>) <state>`, with the task left out until one is
 * pinned.
 *
 * V1 has no RPC bridge between a plugin's server and TUI halves, so the server
 * half (`opencode-activity.ts`) publishes the state and the tasks as namespaced
 * `tui.command.execute` events; this half subscribes to them. Load this file from
 * `tui.json` (see `plugins/README.md`). This is an OpenCode-only affordance:
 * nothing about it is sent to the Activity Monitor or shown in its web UI.
 */
const tui: TuiPlugin = async (api) => {
    const [state, setState] = createSignal<ConnectionState>("pending");
    const [tasks, setTasks] = createSignal<Record<string, string>>({});

    api.event.on("tui.command.execute", (event) => {
        const command = event.properties.command;
        const next = parseState(command);

        if (next !== undefined) {
            setState(next);
        }

        const pinned = parseTask(command);

        if (pinned !== undefined) {
            setTasks((previous) => ({ ...previous, [pinned.sessionID]: pinned.task }));
        }
    });

    api.slots.register({
        order: 210,
        slots: {
            // SlotRenderer takes (context, props): `props` carries the slot's own
            // input, here the session the sidebar is rendering for.
            sidebar_content(_context, props) {
                return <Indicator api={api} state={state()} task={tasks()[props.session_id] ?? ""} />;
            },
        },
    });
};

const plugin: TuiPluginModule & { id: string } = {
    id: "activity-monitor.tui",
    tui,
};

export default plugin;
