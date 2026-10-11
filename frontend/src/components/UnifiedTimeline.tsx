import { Fragment } from "react";
import { Box, Tooltip, Typography } from "@mui/material";
import { colorForKind } from "../lib/timeline";
import { formatDuration, formatTime, parseDate } from "../lib/format";

export type UnifiedState = "closed" | "idle" | "active";

export interface UnifiedSegment {
    start: string;
    end: string;
    state: UnifiedState;
}

export interface UnifiedTimelineEvent {
    id: number | null;
    timestamp: string;
    kind: string;
    actor: string;
    task?: string | null;
    title?: string | null;
    duration_ms?: number | null;
}

export interface UnifiedLane {
    key: string;
    label: string;
    source: string;
    host: string;
    parallelId: number;
    segments: UnifiedSegment[];
    events: UnifiedTimelineEvent[];
}

export interface UserSpan {
    start: string;
    end: string;
    title?: string | null;
    process?: string | null;
}

export interface UserInterval {
    start: string;
    end: string;
}

export interface UserActivity {
    spans: UserSpan[];
    idle: UserInterval[];
    locked: UserInterval[];
}

interface DisplayLane {
    key: string;
    label: string;
    detail: string;
    color: string;
    segments: UnifiedSegment[];
    events: UnifiedTimelineEvent[];
}

const LABEL_WIDTH = 120;
const BAR_HEIGHT = 16;
const ROW_HEIGHT = 40;
const USER_COLOR = "#6366f1";

const STRIP_MIN_MS = 3_600_000;
const KEEP_MS = 15 * 60_000;
const GAP_PCT = 1.8;

function spanMs(span: { start: string; end: string }): number {
    return new Date(span.end).getTime() - new Date(span.start).getTime();
}

function mergeIntervals(intervals: Array<[number, number]>): Array<[number, number]> {
    const sorted = intervals.filter(([start, end]) => end > start).sort((a, b) => a[0] - b[0]);
    const merged: Array<[number, number]> = [];

    for (const [start, end] of sorted) {
        const last = merged[merged.length - 1];

        if (last && start <= last[1]) {
            last[1] = Math.max(last[1], end);
        } else {
            merged.push([start, end]);
        }
    }

    return merged;
}

function covers(merged: Array<[number, number]>, t: number): boolean {
    let lo = 0;
    let hi = merged.length - 1;

    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const [start, end] = merged[mid];

        if (t < start) {
            hi = mid - 1;
        } else if (t >= end) {
            lo = mid + 1;
        } else {
            return true;
        }
    }

    return false;
}

function composeUserSegments(startMs: number, endMs: number, user: UserActivity): UnifiedSegment[] {
    const toInterval = (interval: { start: string; end: string }): [number, number] => [
        Math.max(new Date(interval.start).getTime(), startMs),
        Math.min(new Date(interval.end).getTime(), endMs),
    ];

    const spans = mergeIntervals(user.spans.map(toInterval));
    const idle = mergeIntervals(user.idle.map(toInterval));
    const locked = mergeIntervals(user.locked.map(toInterval));

    const bounds = new Set<number>([startMs, endMs]);

    for (const list of [spans, idle, locked]) {
        for (const [start, end] of list) {
            if (start >= startMs && start <= endMs) {
                bounds.add(start);
            }

            if (end >= startMs && end <= endMs) {
                bounds.add(end);
            }
        }
    }

    const sorted = [...bounds].sort((a, b) => a - b);
    const segments: UnifiedSegment[] = [];

    for (let i = 0; i < sorted.length - 1; i += 1) {
        const left = sorted[i];
        const right = sorted[i + 1];

        if (right <= left) {
            continue;
        }

        const mid = (left + right) / 2;
        const state: UnifiedState = covers(locked, mid)
            ? "closed"
            : covers(idle, mid)
              ? "idle"
              : covers(spans, mid)
                ? "active"
                : "closed";
        const previous = segments[segments.length - 1];

        if (previous && previous.state === state) {
            previous.end = new Date(right).toISOString();
        } else {
            segments.push({ start: new Date(left).toISOString(), end: new Date(right).toISOString(), state });
        }
    }

    return segments;
}

interface GapMark {
    leftPct: number;
    widthPct: number;
    hiddenMs: number;
}

interface TimeCompression {
    startMs: number;
    endMs: number;
    position(ms: number): number;
    isStripped(ms: number): boolean;
    gaps: GapMark[];
}

function buildCompression(startMs: number, endMs: number, busy: Array<[number, number]>): TimeCompression {
    const clamped: Array<[number, number]> = [];

    for (const [start, finish] of busy) {
        const left = Math.max(start, startMs);
        const right = Math.min(finish, endMs);

        if (right > left) {
            clamped.push([left, right]);
        }
    }

    clamped.sort((a, b) => a[0] - b[0]);

    const merged: Array<[number, number]> = [];

    for (const interval of clamped) {
        const last = merged[merged.length - 1];

        if (last && interval[0] <= last[1]) {
            last[1] = Math.max(last[1], interval[1]);
        } else {
            merged.push([...interval]);
        }
    }

    const visibleStart = merged.length > 0 ? merged[0][0] : startMs;
    const visibleEnd = merged.length > 0 ? merged[merged.length - 1][1] : endMs;

    const dead: Array<[number, number]> = [];
    let cursor = visibleStart;

    for (const interval of merged) {
        if (interval[0] > cursor) {
            dead.push([cursor, interval[0]]);
        }

        cursor = Math.max(cursor, interval[1]);
    }

    if (cursor < visibleEnd) {
        dead.push([cursor, visibleEnd]);
    }

    const strips: Array<{ startMs: number; endMs: number }> = [];

    for (const [deadStart, deadEnd] of dead) {
        if (deadEnd - deadStart > STRIP_MIN_MS) {
            const stripStart = deadStart + KEEP_MS;
            const stripEnd = deadEnd - KEEP_MS;

            if (stripEnd > stripStart) {
                strips.push({ startMs: stripStart, endMs: stripEnd });
            }
        }
    }

    const intervals: Array<[number, number]> = [];
    cursor = visibleStart;

    for (const strip of strips) {
        if (strip.startMs > cursor) {
            intervals.push([cursor, strip.startMs]);
        }

        cursor = Math.max(cursor, strip.endMs);
    }

    if (cursor < visibleEnd) {
        intervals.push([cursor, visibleEnd]);
    }

    const totalVisible = intervals.reduce((sum, [left, right]) => sum + (right - left), 0);
    const usable = Math.max(100 - strips.length * GAP_PCT, 40);
    const scale = totalVisible > 0 ? usable / totalVisible : 0;

    const linear: Array<{ startMs: number; endMs: number; startPct: number }> = [];
    const gaps: GapMark[] = [];
    let x = 0;

    for (let i = 0; i < intervals.length; i += 1) {
        const [left, right] = intervals[i];
        linear.push({ startMs: left, endMs: right, startPct: x });
        x += (right - left) * scale;

        if (i < intervals.length - 1) {
            gaps.push({ leftPct: x, widthPct: GAP_PCT, hiddenMs: strips[i].endMs - strips[i].startMs });
            x += GAP_PCT;
        }
    }

    const position = (ms: number): number => {
        for (const interval of linear) {
            if (ms < interval.startMs) {
                return Math.max(interval.startPct - GAP_PCT / 2, 0);
            }

            if (ms <= interval.endMs) {
                return interval.startPct + (ms - interval.startMs) * scale;
            }
        }

        return Math.min(x, 100);
    };

    const isStripped = (ms: number): boolean => strips.some((strip) => ms >= strip.startMs && ms <= strip.endMs);

    return { startMs: visibleStart, endMs: visibleEnd, position, isStripped, gaps };
}

function buildCompressedTicks(
    startMs: number,
    endMs: number,
    compression: TimeCompression,
): Array<{ pct: number; label: string }> {
    const span = endMs - startMs;
    const sameDay = new Date(startMs).toDateString() === new Date(endMs).toDateString();
    let count = 8;
    let ticks: Array<{ pct: number; label: string }> = [];

    while (ticks.length < 3 && count <= 96) {
        ticks = [];

        for (let i = 0; i < count; i += 1) {
            const ms = startMs + (span * i) / (count - 1);

            if (compression.isStripped(ms)) {
                continue;
            }

            ticks.push({
                pct: compression.position(ms),
                label: sameDay
                    ? new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
                    : new Date(ms).toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit" }),
            });
        }

        count *= 2;
    }

    return ticks;
}

function AxisBreak({ gap }: { gap: GapMark }) {
    return (
        <Tooltip arrow title={`Stripped inactivity · ${formatDuration(gap.hiddenMs)}`}>
            <Box
                sx={{
                    position: "absolute",
                    top: "50%",
                    left: `${gap.leftPct}%`,
                    width: `${gap.widthPct}%`,
                    transform: "translateY(-50%)",
                    display: "flex",
                    alignItems: "center",
                    zIndex: 3,
                }}
            >
                <Box sx={{ flex: 1, height: 2, borderRadius: 1, bgcolor: "text.secondary" }} />
                <Box sx={{ width: 5, height: 2, bgcolor: "text.secondary", transform: "rotate(45deg)", mx: "2px" }} />
                <Box sx={{ width: 5, height: 2, bgcolor: "text.secondary", transform: "rotate(45deg)", mx: "2px" }} />
                <Box sx={{ flex: 1, height: 2, borderRadius: 1, bgcolor: "text.secondary" }} />
            </Box>
        </Tooltip>
    );
}

function RowLabel({ label, detail }: { label: string; detail: string }) {
    return (
        <Box sx={{ width: LABEL_WIDTH, flexShrink: 0, pr: 1, textAlign: "right", overflow: "hidden" }}>
            <Typography variant="body2" noWrap title={label}>
                {label}
            </Typography>
            <Typography variant="caption" color="text.secondary" noWrap>
                {detail}
            </Typography>
        </Box>
    );
}

function LaneTrack({
    lane,
    compression,
    onSelect,
}: {
    lane: DisplayLane;
    compression: TimeCompression;
    onSelect?: (event: UnifiedTimelineEvent) => void;
}) {
    return (
        <Box
            sx={{
                position: "relative",
                flex: 1,
                height: ROW_HEIGHT,
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
            {lane.segments.map((segment, index) => {
                const left = compression.position(new Date(segment.start).getTime());
                const right = compression.position(new Date(segment.end).getTime());
                const width = right - left;

                if (segment.state === "closed" || width <= 0) {
                    return null;
                }

                return (
                    <Tooltip
                        key={`${index}-${segment.state}`}
                        arrow
                        title={
                            <Box>
                                <Typography variant="caption" sx={{ display: "block" }}>
                                    {segment.state}
                                </Typography>
                                <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                                    {formatTime(segment.start)} → {formatTime(segment.end)}
                                </Typography>
                            </Box>
                        }
                    >
                        <Box
                            sx={{
                                position: "absolute",
                                top: "50%",
                                left: `${left}%`,
                                width: `${width}%`,
                                height: BAR_HEIGHT,
                                transform: "translateY(-50%)",
                                bgcolor: segment.state === "active" ? lane.color : "transparent",
                                border: segment.state === "active" ? 0 : 1,
                                borderColor: lane.color,
                            }}
                        />
                    </Tooltip>
                );
            })}

            {lane.events.map((event, index) => {
                const ms = new Date(event.timestamp).getTime();

                if (!Number.isFinite(ms) || compression.isStripped(ms)) {
                    return null;
                }

                const left = compression.position(ms);

                return (
                    <Fragment key={`${lane.key}-event-${event.id ?? index}`}>
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
                                    <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                                        {formatTime(event.timestamp)}
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
                                    width: 10,
                                    height: 10,
                                    borderRadius: "50%",
                                    bgcolor: colorForKind(event.kind),
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

            {compression.gaps.map((gap) => (
                <AxisBreak key={`gap-${gap.leftPct}`} gap={gap} />
            ))}
        </Box>
    );
}

function LegendSwatch({ outlined, label }: { outlined?: boolean; label: string }) {
    return (
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Box
                sx={{
                    width: 24,
                    height: 10,
                    bgcolor: outlined ? "transparent" : "text.primary",
                    border: outlined ? 1 : 0,
                    borderColor: "text.primary",
                }}
            />
            <Typography variant="caption" color="text.secondary">
                {label}
            </Typography>
        </Box>
    );
}

export default function UnifiedTimeline({
    start,
    end,
    user,
    lanes,
    onSelect,
    emptyLabel = "No activity in this range.",
}: {
    start: string;
    end: string;
    user: UserActivity;
    lanes: UnifiedLane[];
    onSelect?: (event: UnifiedTimelineEvent) => void;
    emptyLabel?: string;
}) {
    const startDate = parseDate(start);
    const endDate = parseDate(end);

    if (!startDate || !endDate || startDate >= endDate) {
        return null;
    }

    const hasUser = user.spans.length > 0 || user.idle.length > 0 || user.locked.length > 0;

    if (!hasUser && lanes.length === 0) {
        return (
            <Box sx={{ p: 3, textAlign: "center", color: "text.secondary" }}>
                <Typography variant="body2">{emptyLabel}</Typography>
            </Box>
        );
    }

    const startMs = startDate.getTime();
    const endMs = endDate.getTime();

    const displayLanes: DisplayLane[] = [];

    if (hasUser) {
        const userTotal = user.spans.reduce((sum, span) => sum + spanMs(span), 0);

        displayLanes.push({
            key: "user",
            label: "User",
            detail: `${user.spans.length} spans${userTotal > 0 ? ` · ${formatDuration(userTotal)}` : ""}`,
            color: USER_COLOR,
            segments: composeUserSegments(startMs, endMs, user),
            events: [],
        });
    }

    for (const lane of lanes) {
        const activeTime = lane.segments
            .filter((segment) => segment.state !== "closed")
            .reduce((total, segment) => {
                return total + (new Date(segment.end).getTime() - new Date(segment.start).getTime());
            }, 0);

        displayLanes.push({
            key: lane.key,
            label: lane.label,
            detail: `${lane.events.length} events${activeTime > 0 ? ` · ${formatDuration(activeTime)}` : ""}`,
            color: colorForKind(lane.source),
            segments: lane.segments,
            events: lane.events,
        });
    }

    const busy: Array<[number, number]> = [];

    for (const lane of displayLanes) {
        for (const segment of lane.segments) {
            if (segment.state === "closed") {
                continue;
            }

            const left = new Date(segment.start).getTime();
            const right = new Date(segment.end).getTime();

            if (right > left) {
                busy.push([left, right]);
            }
        }
    }

    const compression = buildCompression(startMs, endMs, busy);
    const ticks = buildCompressedTicks(compression.startMs, compression.endMs, compression);

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

            {displayLanes.map((lane) => (
                <Box key={lane.key} sx={{ display: "flex", alignItems: "center", minHeight: ROW_HEIGHT }}>
                    <RowLabel label={lane.label} detail={lane.detail} />
                    <LaneTrack
                        lane={lane}
                        compression={compression}
                        onSelect={lane.events.length > 0 ? onSelect : undefined}
                    />
                </Box>
            ))}

            <Box sx={{ display: "flex", gap: 3, justifyContent: "flex-end", flexWrap: "wrap", mt: 1 }}>
                <LegendSwatch label="Active" />
                <LegendSwatch outlined label="Idle" />
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                    <Box sx={{ width: 24, height: 0, borderTop: 1, borderColor: "text.primary" }} />
                    <Typography variant="caption" color="text.secondary">
                        Closed
                    </Typography>
                </Box>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                    <Box sx={{ display: "flex", alignItems: "center", width: 24 }}>
                        <Box sx={{ flex: 1, height: 2, bgcolor: "text.secondary" }} />
                        <Box sx={{ width: 4, height: 2, bgcolor: "text.secondary", transform: "rotate(45deg)" }} />
                        <Box sx={{ width: 4, height: 2, bgcolor: "text.secondary", transform: "rotate(45deg)" }} />
                        <Box sx={{ flex: 1, height: 2, bgcolor: "text.secondary" }} />
                    </Box>
                    <Typography variant="caption" color="text.secondary">
                        Stripped
                    </Typography>
                </Box>
            </Box>
        </Box>
    );
}
