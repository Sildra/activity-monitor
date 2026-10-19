import { useCallback, useEffect, useRef, useState } from "react";
import { Box, Button, CircularProgress, MenuItem, Paper, Snackbar, TextField, Tooltip, Typography } from "@mui/material";
import CalendarTodayIcon from "@mui/icons-material/CalendarToday";
import CompressIcon from "@mui/icons-material/Compress";
import RefreshIcon from "@mui/icons-material/Refresh";
import { api } from "../api/client";
import UnifiedTimeline, {
    type UnifiedState,
    type UnifiedTimelineEvent,
    type UnifiedLane,
    type UserActivity,
} from "../components/UnifiedTimeline";
import { endOfDay, startOfDay, startOfWeek, toDateTimeInput, toIso } from "../lib/format";
import type { components } from "../api/generated/schema";

type PresenceLane = components["schemas"]["PresenceLane"];
type PresenceTimelineResponse = components["schemas"]["PresenceTimeline"];
type UserTimelineResponse = components["schemas"]["UserTimeline"];
type CodeTimelineResponse = components["schemas"]["CodeTimeline"];

interface TimelineData {
    presence: PresenceTimelineResponse | null;
    user: UserTimelineResponse | null;
    code: CodeTimelineResponse | null;
}

const DEFAULT_START = toDateTimeInput(startOfDay(new Date(Date.now() - 6 * 86400_000)));
const DEFAULT_END = toDateTimeInput(endOfDay(new Date()));

// A calendar glyph carrying the number of days a range preset covers (`1` = today, `7` = this week).
function CalendarDaysIcon({ days }: { days: number }) {
    return (
        <Box sx={{ position: "relative", display: "inline-flex" }}>
            <CalendarTodayIcon fontSize="small" />
            <Box
                component="span"
                sx={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    alignItems: "flex-end",
                    justifyContent: "center",
                    pb: "14%",
                    fontSize: 9,
                    fontWeight: 700,
                    lineHeight: 1,
                }}
            >
                {days}
            </Box>
        </Box>
    );
}

export default function TimelinePage() {
    const [startAt, setStartAt] = useState(DEFAULT_START);
    const [endAt, setEndAt] = useState(DEFAULT_END);
    const [lanes, setLanes] = useState<PresenceLane[]>([]);
    const [user, setUser] = useState<UserTimelineResponse | null>(null);
    const [code, setCode] = useState<CodeTimelineResponse | null>(null);
    const [range, setRange] = useState<{ start: string; end: string }>({ start: "", end: "" });
    const [loading, setLoading] = useState(false);
    const [consolidating, setConsolidating] = useState(false);
    const [consolidateResult, setConsolidateResult] = useState("");
    const [selected, setSelected] = useState<UnifiedTimelineEvent | null>(null);
    const [filterTask, setFilterTask] = useState<string | "">("");
    const [availableTasks, setAvailableTasks] = useState<string[]>([]);
    // Remembers each task's first/last activity so selecting a task can zoom the range even when
    // that activity happened to fall outside the currently loaded range.
    const taskExtents = useRef<Map<string, { min: number; max: number }>>(new Map());

    const noteTasks = useCallback((data: TimelineData) => {
        const note = (task: string | null | undefined, from: number, to: number) => {
            if (!task || !Number.isFinite(from)) {
                return;
            }

            const end = Number.isFinite(to) ? to : from;
            const current = taskExtents.current.get(task);

            taskExtents.current.set(task, {
                min: Math.min(current?.min ?? from, from),
                max: Math.max(current?.max ?? end, end),
            });
        };

        for (const lane of data.presence?.lanes ?? []) {
            for (const event of lane.events) {
                const at = new Date(event.timestamp).getTime();
                note(event.task, at, event.duration_ms ? at + event.duration_ms : at);
            }
        }

        for (const lane of data.user?.lanes ?? []) {
            for (const span of lane.spans) {
                note(lane.task, new Date(span.start).getTime(), new Date(span.end).getTime());
            }
        }

        for (const event of data.code?.events ?? []) {
            const at = new Date(event.timestamp).getTime();
            note(event.task, at, event.duration_ms ? at + event.duration_ms : at);
        }

        setAvailableTasks(Array.from(taskExtents.current.keys()).sort((a, b) => a.localeCompare(b)));
    }, []);

    const applyData = useCallback(
        (data: TimelineData) => {
            if (data.presence) {
                setLanes(data.presence.lanes);
                setRange({ start: data.presence.start, end: data.presence.end });
            }

            setUser(data.user);
            setCode(data.code);
            noteTasks(data);
        },
        [noteTasks],
    );

    const load = useCallback(async (): Promise<TimelineData> => {
        const query = { start: toIso(startAt), end: toIso(endAt) };

        const [presence, userTimeline, codeTimeline] = await Promise.all([
            api.GET("/api/timelines/presence", { params: { query } }),
            api.GET("/api/timelines/user", { params: { query } }),
            api.GET("/api/timelines/code", { params: { query } }),
        ]);

        return {
            presence: presence.data ?? null,
            user: userTimeline.data ?? null,
            code: codeTimeline.data ?? null,
        };
    }, [startAt, endAt]);

    useEffect(() => {
        let cancelled = false;

        load().then((data) => {
            if (cancelled) {
                return;
            }

            applyData(data);
        });

        return () => {
            cancelled = true;
        };
    }, [load, applyData]);

    const refresh = useCallback(() => {
        setLoading(true);
        load()
            .then((data) => applyData(data))
            .finally(() => setLoading(false));
    }, [load, applyData]);

    const consolidate = useCallback(() => {
        setConsolidating(true);
        setConsolidateResult("");

        // The server's idle gap default (1h) governs how dead time is split.
        api.POST("/api/programs/consolidate")
            .then((response) => {
                if (response.data) {
                    setConsolidateResult(
                        `Removed ${response.data.removed} · trimmed ${response.data.trimmed} · split ${response.data.split}`,
                    );
                }
            })
            .finally(() => {
                setConsolidating(false);
                refresh();
            });
    }, [refresh]);

    const applyRange = useCallback((start: Date, end: Date) => {
        setStartAt(toDateTimeInput(start));
        setEndAt(toDateTimeInput(end));
    }, []);

    // Filtering to a task also pins the range to that task's first/last activity, so the timeline
    // shows just that slice of work ("shadowing" every other task).
    const selectTask = useCallback((value: string) => {
        setFilterTask(value);

        if (!value) {
            return;
        }

        const extent = taskExtents.current.get(value);

        if (!extent) {
            return;
        }

        const pad = Math.max(60_000, (extent.max - extent.min) * 0.05);

        setStartAt(toDateTimeInput(new Date(extent.min - pad)));
        setEndAt(toDateTimeInput(new Date(extent.max + pad)));
    }, []);

    const laneCounts = new Map<string, number>();

    for (const lane of lanes) {
        const key = `${lane.source}@${lane.host}`;
        laneCounts.set(key, (laneCounts.get(key) ?? 0) + 1);
    }

    const timelineLanes: UnifiedLane[] = lanes.map((lane) => {
        const base = lane.host ? `${lane.source}@${lane.host}` : lane.source;

        return {
            key: lane.key,
            label: laneCounts.get(`${lane.source}@${lane.host}`) === 1 ? base : `${base} #${lane.parallel_id + 1}`,
            source: lane.source,
            host: lane.host,
            parallelId: lane.parallel_id,
            segments: lane.segments.map((segment) => ({
                start: segment.start,
                end: segment.end,
                state: segment.state as UnifiedState,
            })),
            events: lane.events.map((event) => ({
                id: event.id ?? null,
                timestamp: event.timestamp,
                kind: event.kind,
                actor: event.actor,
                task: event.task,
                title: event.title,
                duration_ms: event.duration_ms,
                data: event.data,
            })),
        };
    });

    // Only the selected task's events survive; presence segments are dropped because they follow a
    // program's whole lifetime, not the task-specific slice.
    const filteredLanes: UnifiedLane[] = filterTask
        ? timelineLanes
              .filter((lane) => lane.events.some((event) => event.task === filterTask))
              .map((lane) => ({
                  ...lane,
                  events: lane.events.filter((event) => event.task === filterTask),
                  segments: [],
              }))
        : timelineLanes;

    const userActivityFiltered: UserActivity = {
        spans: (user?.lanes ?? []).flatMap((lane) =>
            !filterTask || lane.task === filterTask
                ? lane.spans.map((span) => ({
                      start: span.start,
                      end: span.end,
                      title: span.title,
                      task: lane.task,
                  }))
                : [],
        ),
        // Idle/lock intervals are not attributable to a task, so they are hidden while filtering.
        idle: filterTask ? [] : user?.idle ?? [],
        locked: filterTask ? [] : user?.locked ?? [],
    };

    const codeEventsFiltered: UnifiedTimelineEvent[] = (code?.events ?? [])
        .filter((event) => !filterTask || event.task === filterTask)
        .map((event) => ({
            id: event.id ?? null,
            timestamp: event.timestamp,
            kind: event.kind,
            actor: event.actor,
            task: event.task,
            title: event.title,
            duration_ms: event.duration_ms,
        }));

    return (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, height: "100%", p: 2, overflow: "auto" }}>
            <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 2 }}>
                <Typography variant="h6">Timeline</Typography>
                <TextField
                    label="From"
                    type="datetime-local"
                    size="small"
                    value={startAt}
                    onChange={(e) => setStartAt(e.target.value)}
                    slotProps={{ inputLabel: { shrink: true } }}
                />
                <TextField
                    label="To"
                    type="datetime-local"
                    size="small"
                    value={endAt}
                    onChange={(e) => setEndAt(e.target.value)}
                    slotProps={{ inputLabel: { shrink: true } }}
                />
                <Tooltip title="Today">
                    <Button
                        variant="text"
                        size="small"
                        onClick={() => applyRange(startOfDay(new Date()), endOfDay(new Date()))}
                        sx={{ minWidth: 0, px: 1, height: 40 }}
                    >
                        <CalendarDaysIcon days={1} />
                    </Button>
                </Tooltip>
                <Tooltip title="This week">
                    <Button
                        variant="text"
                        size="small"
                        onClick={() => applyRange(startOfWeek(new Date()), endOfDay(new Date()))}
                        sx={{ minWidth: 0, px: 1, height: 40 }}
                    >
                        <CalendarDaysIcon days={7} />
                    </Button>
                </Tooltip>
                <Tooltip title="Refresh">
                    <span>
                        <Button
                            variant="outlined"
                            size="small"
                            onClick={refresh}
                            disabled={loading}
                            sx={{ minWidth: 0, px: 1, height: 40 }}
                        >
                            <RefreshIcon fontSize="small" />
                        </Button>
                    </span>
                </Tooltip>
                <Tooltip title="Consolidate">
                    <span>
                        <Button
                            variant="outlined"
                            color="secondary"
                            size="small"
                            onClick={consolidate}
                            disabled={consolidating}
                            sx={{ minWidth: 0, px: 1, height: 40 }}
                        >
                            <CompressIcon fontSize="small" sx={{ transform: "rotate(90deg)" }} />
                        </Button>
                    </span>
                </Tooltip>
                {consolidating && <CircularProgress size={20} />}
                {loading && <CircularProgress size={20} />}
                <TextField
                    select
                    label="Active task"
                    size="small"
                    value={filterTask}
                    onChange={(e) => selectTask(e.target.value)}
                    sx={{ minWidth: 200 }}
                >
                    <MenuItem value="">All tasks</MenuItem>
                    {availableTasks.map((t) => (
                        <MenuItem key={t} value={t}>
                            {t}
                        </MenuItem>
                    ))}
                </TextField>
            </Box>

            <Paper variant="outlined" sx={{ p: 2, flex: 1, minHeight: 200, overflow: "auto" }}>
                <Typography variant="subtitle1" sx={{ mb: 1 }}>
                    Activity
                </Typography>
                <UnifiedTimeline
                    start={range.start}
                    end={range.end}
                    user={userActivityFiltered}
                    lanes={filteredLanes}
                    code={codeEventsFiltered}
                    onSelect={setSelected}
                    emptyLabel="No activity in the selected range."
                />
            </Paper>

            {selected && (
                <Paper variant="outlined" sx={{ p: 2 }}>
                    <Typography variant="subtitle2">{selected.title ?? selected.kind}</Typography>
                    <Typography variant="caption" color="text.secondary">
                        {selected.actor} · {selected.kind} · {new Date(selected.timestamp).toLocaleString()}
                        {selected.task ? ` · ${selected.task}` : ""}
                    </Typography>
                </Paper>
            )}

            <Snackbar
                open={consolidateResult !== ""}
                autoHideDuration={4000}
                onClose={() => setConsolidateResult("")}
                message={consolidateResult}
            />
        </Box>
    );
}
