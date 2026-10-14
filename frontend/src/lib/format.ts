export function parseDate(value: string | null | undefined): Date | null {
    if (!value) {
        return null;
    }

    const date = new Date(value);

    return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string | Date | null | undefined): string {
    const date = typeof value === "string" ? parseDate(value) : value;

    if (!date) {
        return "-";
    }

    return date.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    });
}

export function formatTime(value: string | Date | null | undefined): string {
    const date = typeof value === "string" ? parseDate(value) : value;

    if (!date) {
        return "-";
    }

    return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function formatDuration(ms: number | null | undefined): string {
    if (ms === null || ms === undefined || ms <= 0) {
        return "";
    }

    if (ms < 1000) {
        return `${Math.round(ms)} ms`;
    }

    const seconds = ms / 1000;

    if (seconds < 60) {
        return `${seconds.toFixed(1)} s`;
    }

    const minutes = seconds / 60;

    if (minutes < 60) {
        return `${minutes.toFixed(1)} min`;
    }

    return `${(minutes / 60).toFixed(1)} h`;
}

export function startOfDay(date: Date): Date {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, 0, 0, 0);
}

export function endOfDay(date: Date): Date {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 23, 59, 59, 999);
}

export function startOfWeek(date: Date): Date {
    const monday = (date.getDay() + 6) % 7;

    return startOfDay(new Date(date.getFullYear(), date.getMonth(), date.getDate() - monday));
}

export function toDateTimeInput(date: Date): string {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);

    return local.toISOString().slice(0, 16);
}

export function toIso(value: string): string | undefined {
    if (!value) {
        return undefined;
    }

    const date = new Date(value);

    return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}
