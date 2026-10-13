import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Box,
    Button,
    Chip,
    CircularProgress,
    Divider,
    List,
    ListItemButton,
    ListItemText,
    MenuItem,
    Paper,
    TextField,
    Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { api } from "../api/client";
import Timeline, { type TimelineEvent } from "../components/Timeline";
import { groupBy, type Lane } from "../lib/timeline";
import { endOfDay, formatDateTime, formatTime, startOfDay, toDateInput } from "../lib/format";
import type { components } from "../api/generated/schema";

type EventOut = components["schemas"]["EventOut"];
type TaskSummary = components["schemas"]["TaskSummary"];
type SourceInfo = components["schemas"]["SourceInfo"];

interface TaskData {
    events: EventOut[];
    summaries: TaskSummary[];
}

const TODAY = toDateInput(new Date());
const WEEK_AGO = toDateInput(new Date(Date.now() - 6 * 86400_000));

function toTimelineEvent(event: EventOut): TimelineEvent {
    return {
        id: event.id ?? null,
        timestamp: event.timestamp,
        kind: event.kind,
        actor: event.actor,
        task: event.task,
        title: event.title,
        duration_ms: event.duration_ms,
    };
}

export default function TaskPage() {
    const [q, setQ] = useState("");
    const [source, setSource] = useState("");
    const [actor, setActor] = useState("");
    const [kind, setKind] = useState("");
    const [task, setTask] = useState("");
    const [startDate, setStartDate] = useState(WEEK_AGO);
    const [endDate, setEndDate] = useState(TODAY);

    const [sources, setSources] = useState<SourceInfo[]>([]);
    const [actors, setActors] = useState<string[]>([]);
    const [kinds, setKinds] = useState<string[]>([]);

    const [events, setEvents] = useState<EventOut[]>([]);
    const [summaries, setSummaries] = useState<TaskSummary[]>([]);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        let cancelled = false;

        Promise.all([api.GET("/api/sources"), api.GET("/api/actors"), api.GET("/api/kinds")]).then(
            ([sourceResult, actorResult, kindResult]) => {
                if (cancelled) {
                    return;
                }

                if (sourceResult.data) {
                    setSources(sourceResult.data);
                }

                if (actorResult.data) {
                    setActors(actorResult.data);
                }

                if (kindResult.data) {
                    setKinds(kindResult.data);
                }
            },
        );

        return () => {
            cancelled = true;
        };
    }, []);

    const load = useCallback(async (): Promise<TaskData | null> => {
        const range = { start: startOfDay(startDate), end: endOfDay(endDate) };
        const [tasksResult, summaryResult] = await Promise.all([
            api.GET("/api/tasks", {
                params: {
                    query: {
                        q: q || undefined,
                        source: source || undefined,
                        actor: actor || undefined,
                        kind: kind || undefined,
                        task: task || undefined,
                        ...range,
                    },
                },
            }),
            api.GET("/api/tasks/summary", { params: { query: { q: q || undefined, ...range } } }),
        ]);

        if (!tasksResult.data || !summaryResult.data) {
            return null;
        }

        return { events: tasksResult.data, summaries: summaryResult.data };
    }, [q, source, actor, kind, task, startDate, endDate]);

    useEffect(() => {
        let cancelled = false;

        load().then((data) => {
            if (!cancelled && data) {
                setEvents(data.events);
                setSummaries(data.summaries);
            }
        });

        return () => {
            cancelled = true;
        };
    }, [load]);

    const refresh = useCallback(() => {
        setLoading(true);
        load()
            .then((data) => {
                if (data) {
                    setEvents(data.events);
                    setSummaries(data.summaries);
                }
            })
            .finally(() => setLoading(false));
    }, [load]);

    const lanes: Lane<TimelineEvent>[] = useMemo(() => {
        const grouped = groupBy(events, (event) => event.actor);

        return Array.from(grouped.entries())
            .map(([name, items]) => ({
                key: name,
                label: name,
                events: items.map(toTimelineEvent).sort((a, b) => a.timestamp.localeCompare(b.timestamp)),
            }))
            .sort((a, b) => a.label.localeCompare(b.label));
    }, [events]);

    return (
        <Box sx={{ display: "flex", gap: 2, height: "100%", p: 2, overflow: "hidden" }}>
            <Paper
                variant="outlined"
                sx={{ width: 280, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}
            >
                <Box sx={{ p: 1.5 }}>
                    <Typography variant="subtitle2" gutterBottom>
                        Tasks
                    </Typography>
                    <TextField
                        fullWidth
                        size="small"
                        placeholder="Search tasks…"
                        value={q}
                        onChange={(e) => setQ(e.target.value)}
                    />
                    {task && (
                        <Chip sx={{ mt: 1 }} size="small" label={task} onDelete={() => setTask("")} color="primary" />
                    )}
                </Box>
                <Divider />
                <List dense sx={{ overflow: "auto", flex: 1 }}>
                    {summaries.map((summary) => (
                        <ListItemButton
                            key={summary.task}
                            selected={task === summary.task}
                            onClick={() => setTask(task === summary.task ? "" : summary.task)}
                        >
                            <ListItemText
                                primary={summary.task}
                                secondary={`${summary.count} · ${formatDateTime(summary.last)}`}
                                slotProps={{ primary: { noWrap: true }, secondary: { noWrap: true } }}
                            />
                        </ListItemButton>
                    ))}
                    {summaries.length === 0 && (
                        <Typography variant="caption" color="text.secondary" sx={{ p: 1.5 }}>
                            No tasks found.
                        </Typography>
                    )}
                </List>
            </Paper>

            <Box sx={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 1.5, overflow: "hidden" }}>
                <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, alignItems: "center" }}>
                    <TextField
                        label="From"
                        type="date"
                        size="small"
                        value={startDate}
                        onChange={(e) => setStartDate(e.target.value)}
                        slotProps={{ inputLabel: { shrink: true } }}
                    />
                    <TextField
                        label="To"
                        type="date"
                        size="small"
                        value={endDate}
                        onChange={(e) => setEndDate(e.target.value)}
                        slotProps={{ inputLabel: { shrink: true } }}
                    />
                    <TextField
                        select
                        label="Source"
                        size="small"
                        value={source}
                        onChange={(e) => setSource(e.target.value)}
                        sx={{ minWidth: 130 }}
                    >
                        <MenuItem value="">All</MenuItem>
                        {sources.map((item) => (
                            <MenuItem key={item.name} value={item.name}>
                                {item.name}
                            </MenuItem>
                        ))}
                    </TextField>
                    <TextField
                        select
                        label="Actor"
                        size="small"
                        value={actor}
                        onChange={(e) => setActor(e.target.value)}
                        sx={{ minWidth: 130 }}
                    >
                        <MenuItem value="">All</MenuItem>
                        {actors.map((item) => (
                            <MenuItem key={item} value={item}>
                                {item}
                            </MenuItem>
                        ))}
                    </TextField>
                    <TextField
                        select
                        label="Kind"
                        size="small"
                        value={kind}
                        onChange={(e) => setKind(e.target.value)}
                        sx={{ minWidth: 130 }}
                    >
                        <MenuItem value="">All</MenuItem>
                        {kinds.map((item) => (
                            <MenuItem key={item} value={item}>
                                {item}
                            </MenuItem>
                        ))}
                    </TextField>
                    <Button variant="outlined" startIcon={<RefreshIcon />} onClick={refresh} disabled={loading}>
                        Refresh
                    </Button>
                    {loading && <CircularProgress size={20} />}
                </Box>

                <Paper variant="outlined" sx={{ p: 2 }}>
                    <Timeline lanes={lanes} emptyLabel="No matching events." />
                </Paper>

                <Paper variant="outlined" sx={{ p: 1, flex: 1, overflow: "auto" }}>
                    <List dense>
                        {events.map((event, index) => (
                            <ListItemButton key={event.id ?? index} selected={false}>
                                <Box sx={{ display: "flex", gap: 1, alignItems: "center", width: "100%", minWidth: 0 }}>
                                    <Typography
                                        variant="caption"
                                        color="text.secondary"
                                        sx={{ width: 90, flexShrink: 0 }}
                                    >
                                        {formatTime(event.timestamp)}
                                    </Typography>
                                    <Chip size="small" label={event.kind} sx={{ flexShrink: 0 }} />
                                    <Typography variant="body2" noWrap sx={{ flex: 1, minWidth: 0 }}>
                                        {event.title ?? event.detail ?? "-"}
                                    </Typography>
                                    <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
                                        {event.actor}
                                        {event.host ? ` @${event.host}` : ""}
                                        {event.task ? ` · ${event.task}` : ""}
                                    </Typography>
                                </Box>
                            </ListItemButton>
                        ))}
                        {events.length === 0 && (
                            <Typography variant="caption" color="text.secondary" sx={{ p: 1.5 }}>
                                No events.
                            </Typography>
                        )}
                    </List>
                </Paper>
            </Box>
        </Box>
    );
}
