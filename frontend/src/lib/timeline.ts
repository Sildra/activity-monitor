import { parseDate } from "./format";

export interface TimePoint {
    timestamp: string;
}

export interface Lane<T extends TimePoint> {
    key: string;
    label: string;
    events: T[];
}

export interface Tick {
    pct: number;
    label: string;
}

const KIND_COLORS = [
    "#0ea5e9",
    "#22c55e",
    "#f59e0b",
    "#ef4444",
    "#a855f7",
    "#14b8a6",
    "#ec4899",
    "#84cc16",
];

export function colorForKind(kind: string): string {
    let hash = 0;

    for (let i = 0; i < kind.length; i++) {
        hash = (hash * 31 + kind.charCodeAt(i)) >>> 0;
    }

    return KIND_COLORS[hash % KIND_COLORS.length];
}

export function timeDomain<T extends TimePoint>(events: T[]): [Date, Date] {
    const times = events
        .map((event) => parseDate(event.timestamp)?.getTime())
        .filter((time): time is number => time !== undefined);

    if (times.length === 0) {
        const now = Date.now();

        return [new Date(now - 3600_000), new Date(now)];
    }

    const min = Math.min(...times);
    const max = Math.max(...times);
    const padding = Math.max((max - min) * 0.05, 60_000);

    return [new Date(min - padding), new Date(max + padding)];
}

export function positionPct(timestamp: string, start: Date, end: Date): number {
    const date = parseDate(timestamp);

    if (!date) {
        return 0;
    }

    const span = end.getTime() - start.getTime();

    if (span <= 0) {
        return 0;
    }

    return Math.min(100, Math.max(0, ((date.getTime() - start.getTime()) / span) * 100));
}

export function buildTicks(start: Date, end: Date, count = 6): Tick[] {
    const span = end.getTime() - start.getTime();

    if (span <= 0 || count < 2) {
        return [];
    }

    const ticks: Tick[] = [];
    const sameDay = start.toDateString() === end.toDateString();

    for (let i = 0; i < count; i++) {
        const pct = (i / (count - 1)) * 100;
        const date = new Date(start.getTime() + (span * i) / (count - 1));
        const label = sameDay
            ? date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
            : date.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit" });

        ticks.push({ pct, label });
    }

    return ticks;
}

export function groupBy<T, K extends string>(items: T[], key: (item: T) => K): Map<K, T[]> {
    const groups = new Map<K, T[]>();

    for (const item of items) {
        const group = key(item);
        const bucket = groups.get(group);

        if (bucket) {
            bucket.push(item);
        } else {
            groups.set(group, [item]);
        }
    }

    return groups;
}
