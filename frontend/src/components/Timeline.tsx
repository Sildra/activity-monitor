import { Fragment } from "react";
import { Box, Tooltip, Typography } from "@mui/material";
import { buildTicks, colorForKind, positionPct, timeDomain, type Lane } from "../lib/timeline";
import { formatDuration, formatTime } from "../lib/format";

export interface TimelineEvent {
    id: number | null;
    timestamp: string;
    kind: string;
    actor: string;
    task?: string | null;
    title?: string | null;
    duration_ms?: number | null;
}

const LABEL_WIDTH = 120;

export default function Timeline({
    lanes,
    onSelect,
    emptyLabel = "No events in this range.",
}: {
    lanes: Lane<TimelineEvent>[];
    onSelect?: (event: TimelineEvent) => void;
    emptyLabel?: string;
}) {
    const events = lanes.flatMap((lane) => lane.events);

    if (events.length === 0) {
        return (
            <Box sx={{ p: 3, textAlign: "center", color: "text.secondary" }}>
                <Typography variant="body2">{emptyLabel}</Typography>
            </Box>
        );
    }

    const [start, end] = timeDomain(events);
    const ticks = buildTicks(start, end);

    return (
        <Box sx={{ width: "100%" }}>
            <Box sx={{ display: "flex", mb: 0.5 }}>
                <Box sx={{ width: LABEL_WIDTH, flexShrink: 0 }} />
                <Box sx={{ position: "relative", flex: 1, height: 18 }}>
                    {ticks.map((tick) => (
                        <Typography
                            key={tick.pct}
                            variant="caption"
                            color="text.secondary"
                            sx={{
                                position: "absolute",
                                left: `${tick.pct}%`,
                                transform: "translateX(-50%)",
                                whiteSpace: "nowrap",
                            }}
                        >
                            {tick.label}
                        </Typography>
                    ))}
                </Box>
            </Box>

            {lanes.map((lane) => (
                <Box key={lane.key} sx={{ display: "flex", alignItems: "center", minHeight: 40 }}>
                    <Box sx={{ width: LABEL_WIDTH, flexShrink: 0, pr: 1, textAlign: "right", overflow: "hidden" }}>
                        <Typography variant="body2" noWrap title={lane.label}>
                            {lane.label}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                            {lane.events.length}
                        </Typography>
                    </Box>
                    <Box
                        sx={{
                            position: "relative",
                            flex: 1,
                            height: 40,
                            "&::before": {
                                content: '""',
                                position: "absolute",
                                top: "50%",
                                left: 0,
                                right: 0,
                                borderTop: 1,
                                borderColor: "divider",
                            },
                        }}
                    >
                        {lane.events.map((event, index) => {
                            const left = positionPct(event.timestamp, start, end);
                            const color = colorForKind(event.kind);
                            const endPct =
                                event.duration_ms && event.duration_ms > 0
                                    ? positionPct(
                                          new Date(
                                              new Date(event.timestamp).getTime() + event.duration_ms,
                                          ).toISOString(),
                                          start,
                                          end,
                                      )
                                    : left;
                            const spanPct = Math.max(endPct - left, 0);

                            return (
                                <Fragment key={`${lane.key}-${event.id ?? index}`}>
                                    {spanPct > 0 && (
                                        <Box
                                            sx={{
                                                position: "absolute",
                                                top: "50%",
                                                left: `${left}%`,
                                                width: `${spanPct}%`,
                                                height: 6,
                                                transform: "translateY(-50%)",
                                                bgcolor: color,
                                                opacity: 0.3,
                                            }}
                                        />
                                    )}
                                    <Tooltip
                                        arrow
                                        title={
                                            <Box>
                                                <Typography variant="caption" sx={{ display: "block" }}>
                                                    {event.actor} · {event.kind}
                                                </Typography>
                                                {event.title && (
                                                    <Typography variant="caption" sx={{ display: "block" }}>
                                                        {event.title}
                                                    </Typography>
                                                )}
                                                <Typography
                                                    variant="caption"
                                                    color="text.secondary"
                                                    sx={{ display: "block" }}
                                                >
                                                    {formatTime(event.timestamp)}
                                                    {event.task ? ` · ${event.task}` : ""}
                                                    {formatDuration(event.duration_ms)
                                                        ? ` · ${formatDuration(event.duration_ms)}`
                                                        : ""}
                                                </Typography>
                                            </Box>
                                        }
                                    >
                                        <Box
                                            onClick={() => onSelect?.(event)}
                                            sx={{
                                                position: "absolute",
                                                top: "50%",
                                                left: `${left}%`,
                                                transform: "translate(-50%, -50%)",
                                                width: 12,
                                                height: 12,
                                                borderRadius: "50%",
                                                bgcolor: color,
                                                border: "2px solid",
                                                borderColor: "background.paper",
                                                cursor: onSelect ? "pointer" : "default",
                                                zIndex: 2,
                                                transition: "transform 120ms",
                                                "&:hover": { transform: "translate(-50%, -50%) scale(1.5)" },
                                            }}
                                        />
                                    </Tooltip>
                                </Fragment>
                            );
                        })}
                    </Box>
                </Box>
            ))}
        </Box>
    );
}
