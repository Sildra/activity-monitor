import { useCallback, useEffect, useState } from "react";
import { Box, Button, CircularProgress, Paper, TextField, Typography } from "@mui/material";
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

const DEFAULT_START = toDateTimeInput(startOfDay(new Date(Date.now() - 6 * 86400_000)));
const DEFAULT_END = toDateTimeInput(endOfDay(new Date()));

export default function TimelinePage() {
    const [startAt, setStartAt] = useState(DEFAULT_START);
    const [endAt, setEndAt] = useState(DEFAULT_END);
    const [lanes, setLanes] = useState<PresenceLane[]>([]);
    const [user, setUser] = useState<UserTimelineResponse | null>(null);
    const [code, setCode] = useState<CodeTimelineResponse | null>(null);
    const [range, setRange] = useState<{ start: string; end: string }>({ start: "", end: "" });
    const [loading, setLoading] = useState(false);
    const [idleHours, setIdleHours] = useState("1");
    const [consolidating, setConsolidating] = useState(false);
    const [consolidateResult, setConsolidateResult] = useState("");
    const [selected, setSelected] = useState<UnifiedTimelineEvent | null>(null);

    const load = useCallback(async (): Promise<{
        presence: PresenceTimelineResponse | null;
        user: UserTimelineResponse | null;
        code: CodeTimelineResponse | null;
    }> => {
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

            if (data.presence) {
                setLanes(data.presence.lanes);
                setRange({ start: data.presence.start, end: data.presence.end });
            }

            setUser(data.user);
            setCode(data.code);
        });

        return () => {
            cancelled = true;
        };
    }, [load]);

    const refresh = useCallback(() => {
        setLoading(true);
        load()
            .then((data) => {
                if (data.presence) {
                    setLanes(data.presence.lanes);
                    setRange({ start: data.presence.start, end: data.presence.end });
                }

                setUser(data.user);
                setCode(data.code);
            })
            .finally(() => setLoading(false));
    }, [load]);

    const consolidate = useCallback(() => {
        const idle_seconds = Math.max(1, Math.round((Number(idleHours) || 1) * 3600));

        setConsolidating(true);
        setConsolidateResult("");

        api.POST("/api/programs/consolidate", { params: { query: { idle_seconds } } })
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
    }, [idleHours, refresh]);

    const applyRange = useCallback((start: Date, end: Date) => {
        setStartAt(toDateTimeInput(start));
        setEndAt(toDateTimeInput(end));
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

    const userActivity: UserActivity = {
        spans: (user?.lanes ?? []).flatMap((lane) =>
            lane.spans.map((span) => ({
                start: span.start,
                end: span.end,
                title: span.title,
            })),
        ),
        idle: user?.idle ?? [],
        locked: user?.locked ?? [],
    };

    const codeEvents: UnifiedTimelineEvent[] = (code?.events ?? []).map((event) => ({
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
                <Button
                    size="small"
                    variant="text"
                    onClick={() => applyRange(startOfDay(new Date()), endOfDay(new Date()))}
                >
                    Today
                </Button>
                <Button
                    size="small"
                    variant="text"
                    onClick={() => applyRange(startOfWeek(new Date()), endOfDay(new Date()))}
                >
                    This week
                </Button>
                <Button variant="outlined" startIcon={<RefreshIcon />} onClick={refresh} disabled={loading}>
                    Refresh
                </Button>
                <TextField
                    label="Idle (h)"
                    type="number"
                    size="small"
                    value={idleHours}
                    onChange={(e) => setIdleHours(e.target.value)}
                    slotProps={{ htmlInput: { min: 0.25, step: 0.25 }, inputLabel: { shrink: true } }}
                    sx={{ width: 110 }}
                />
                <Button
                    variant="outlined"
                    color="secondary"
                    startIcon={<CompressIcon />}
                    onClick={consolidate}
                    disabled={consolidating}
                >
                    Consolidate
                </Button>
                {consolidating && <CircularProgress size={20} />}
                {consolidateResult && (
                    <Typography variant="caption" color="text.secondary">
                        {consolidateResult}
                    </Typography>
                )}
                {loading && <CircularProgress size={20} />}
            </Box>

            <Paper variant="outlined" sx={{ p: 2, flex: 1, minHeight: 200, overflow: "auto" }}>
                <Typography variant="subtitle1" sx={{ mb: 1 }}>
                    Activity
                </Typography>
                <UnifiedTimeline
                    start={range.start}
                    end={range.end}
                    user={userActivity}
                    lanes={timelineLanes}
                    code={codeEvents}
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
        </Box>
    );
}
