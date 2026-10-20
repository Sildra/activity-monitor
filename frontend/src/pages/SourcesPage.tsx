import { useCallback, useEffect, useState } from "react";
import {
    Box,
    Button,
    Chip,
    CircularProgress,
    Divider,
    List,
    ListItem,
    Paper,
    Snackbar,
    Typography,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import { api } from "../api/client";
import type { components } from "../api/generated/schema";

type SourceInfo = components["schemas"]["SourceInfo"];
type StatsOut = components["schemas"]["StatsOut"];

interface SourcesData {
    sources: SourceInfo[];
    stats: StatsOut | null;
}

export default function SourcesPage() {
    const [sources, setSources] = useState<SourceInfo[]>([]);
    const [stats, setStats] = useState<StatsOut | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [message, setMessage] = useState<string | null>(null);

    const load = useCallback(async (): Promise<SourcesData | null> => {
        const [sourcesResult, statsResult] = await Promise.all([api.GET("/api/sources"), api.GET("/api/stats")]);

        return { sources: sourcesResult.data ?? [], stats: statsResult.data ?? null };
    }, []);

    useEffect(() => {
        let cancelled = false;

        load().then((data) => {
            if (!cancelled && data) {
                setSources(data.sources);
                setStats(data.stats);
            }
        });

        return () => {
            cancelled = true;
        };
    }, [load]);

    const fetchSource = useCallback(
        async (name: string) => {
            setBusy(name);

            try {
                const { data, error } = await api.POST("/api/sources/{name}/fetch", {
                    params: { path: { name } },
                });

                if (error) {
                    setMessage(`Fetch failed for '${name}'`);
                } else if (data) {
                    setMessage(`'${name}': fetched ${data.fetched}, stored ${data.accepted} new event(s)`);
                }

                const refreshed = await load();

                if (refreshed) {
                    setSources(refreshed.sources);
                    setStats(refreshed.stats);
                }
            } finally {
                setBusy(null);
            }
        },
        [load],
    );

    return (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, p: 2, height: "100%", overflow: "auto" }}>
            <Typography variant="h6">Sources</Typography>

            {stats && (
                <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
                    <Chip label={`${stats.events} events`} />
                    <Chip label={`${stats.sources} sources`} />
                    <Chip label={`${stats.actors} actors`} />
                    <Chip label={`${stats.tasks} tasks`} />
                </Box>
            )}

            <Paper variant="outlined">
                <List>
                    {sources.map((source) => (
                        <ListItem key={source.name} sx={{ gap: 1 }}>
                            <Typography sx={{ width: 140, fontWeight: 500 }}>{source.name}</Typography>
                            <Chip size="small" label={source.kind} />
                            <Chip size="small" variant="outlined" label={source.pull ? "pull" : "push"} />
                            <Box sx={{ flex: 1 }} />
                            {source.pull && (
                                <Button
                                    size="small"
                                    startIcon={<DownloadIcon />}
                                    disabled={busy === source.name}
                                    onClick={() => fetchSource(source.name)}
                                >
                                    Fetch
                                </Button>
                            )}
                        </ListItem>
                    ))}
                    {sources.length === 0 && (
                        <ListItem>
                            <Typography variant="body2" color="text.secondary">
                                No sources configured. Add them under `sources:` in config.yaml.
                            </Typography>
                        </ListItem>
                    )}
                </List>
            </Paper>

            {busy && <CircularProgress size={20} />}

            <Divider />

            <Box>
                <Typography variant="subtitle2" gutterBottom>
                    Push events
                </Typography>
                <Typography variant="body2" color="text.secondary" gutterBottom>
                    Push-based sources (e.g. an opencode plugin) post events with the source name in the payload:
                </Typography>
                <Box
                    component="pre"
                    sx={{
                        m: 0,
                        p: 1.5,
                        bgcolor: "action.hover",
                        borderRadius: 1,
                        overflow: "auto",
                        fontSize: 12,
                    }}
                >
{`curl -X POST http://localhost:8001/api/ingest/event \\
  -H "Content-Type: application/json" \\
  -d '{"source": "opencode", "events": [
    {"id": "abc", "kind": "tool_call", "actor": "agent-a",
     "task": "feature-x", "timestamp": "2026-10-10T09:00:00",
     "title": "edited main.py", "tags": ["edit"]}
  ]}'`}
                </Box>
            </Box>

            <Snackbar
                open={message !== null}
                autoHideDuration={4000}
                onClose={() => setMessage(null)}
                message={message ?? ""}
            />
        </Box>
    );
}
