import { createHash } from "node:crypto";
import { hostname } from "node:os";
import type { Plugin } from "@opencode-ai/plugin";

type Options = {
    baseUrl?: string;
    source?: string;
    actor?: string;
    truncate?: number;
    heartbeatMs?: number;
    programId?: string;
};

type Pending = {
    messageID: string;
    title: string;
    task: string;
    start: number;
};

const envVar = (name: string): string | undefined => {
    const processEnv = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;

    return processEnv?.[name];
};

const getPid = (): string => {
    const pid = (globalThis as { process?: { pid?: number } }).process?.pid;

    return pid !== undefined ? String(pid) : "0";
};

const defaultActor = (): string =>
    envVar("ACTIVITY_MONITOR_ACTOR") ?? envVar("USER") ?? envVar("USERNAME") ?? "opencode";

const stableId = (...parts: string[]): string => createHash("sha1").update(parts.join("\u001f")).digest("hex");

const firstTask = (text: string, max: number): string => {
    const line = text.split("\n").map((part) => part.trim()).find(Boolean) ?? text.trim();

    return line.length > max ? `${line.slice(0, max)}\u2026` : line;
};

const projectName = (directory: string): string => directory.split(/[\\/]/).filter(Boolean).pop() ?? "opencode";

export const OpenCodeActivity: Plugin = async ({ directory, client }, options) => {
    const port = envVar("ACTIVITY_MONITOR_PORT") ?? "8001";

    const opts: Options = {
        baseUrl: envVar("ACTIVITY_MONITOR_BASE_URL") ?? `http://localhost:${port}`,
        source: envVar("ACTIVITY_MONITOR_SOURCE") ?? "opencode",
        actor: defaultActor(),
        truncate: 120,
        heartbeatMs: 60_000,
        ...(options ?? {}),
    };
    const source = opts.source ?? "opencode";
    const programId = opts.programId ?? getPid();
    const host = hostname();
    const pending = new Map<string, Pending>();

    let lastError = "";

    const report = (message: string): void => {
        if (!message || message === lastError) {
            return;
        }

        lastError = message;

        void client.tui
            .showToast({ body: { title: "Activity Monitor", message, variant: "error", duration: 6000 } })
            .catch(() => undefined);
    };

    const post = async (path: string, body: unknown, attempts = 1, backoffMs = 750): Promise<boolean> => {
        let failure = "";

        for (let attempt = 0; attempt < attempts; attempt += 1) {
            try {
                const response = await fetch(`${opts.baseUrl}${path}`, {
                    method: "POST",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify(body),
                    signal: AbortSignal.timeout(3000),
                });

                if (response.ok) {
                    lastError = "";

                    return true;
                }

                failure = `${path} rejected (${response.status})`;
            } catch (error) {
                failure = `${path} failed: ${(error as Error).message}`;
            }

            await new Promise((resolve) => setTimeout(resolve, backoffMs));
        }

        report(failure);

        return false;
    };

    const ingest = (event: Record<string, unknown>): Promise<boolean> =>
        post(`/api/ingest/${encodeURIComponent(source)}`, event);

    const beat = (): Promise<boolean> => post("/api/programs/beat", { program_id: programId });

    let interval: ReturnType<typeof setInterval> | undefined;

    const programEvents = (): { id: string; source: string; host: string } => ({ id: programId, source, host });

    await post(
        "/api/programs/start",
        { program_id: programId, source, host, actor: opts.actor },
        8,
        1000,
    );

    interval = setInterval(() => {
        beat();
    }, opts.heartbeatMs ?? 60_000);

    return {
        "chat.message": async (input, output) => {
            const text = output.parts
                .filter((part) => part.type === "text")
                .map((part) => part.text)
                .join("\n")
                .trim();

            if (!text) {
                return;
            }

            const start = output.message.time?.created ?? Date.now();
            const title = firstTask(text, opts.truncate ?? 120);

            pending.set(output.message.sessionID, {
                messageID: output.message.id,
                title,
                task: projectName(directory),
                start,
            });

            await ingest({
                id: stableId("prompt", "begin", input.sessionID, output.message.id),
                kind: "prompt.begin",
                timestamp: new Date(start).toISOString(),
                actor: opts.actor,
                task: projectName(directory),
                title: `prompt: ${title}`,
                detail: text,
                tags: ["opencode", "prompt"],
                data: {
                    sessionID: output.message.sessionID,
                    messageID: output.message.id,
                    agent: output.message.agent,
                    model: `${output.message.model.providerID}/${output.message.model.modelID}`,
                    directory,
                },
                program: programEvents(),
            });
        },
        event: async ({ event }) => {
            if (event.type !== "session.idle" && event.type !== "session.error") {
                return;
            }

            const sessionID = event.properties.sessionID;
            const record = sessionID !== undefined ? pending.get(sessionID) : undefined;

            if (record === undefined) {
                return;
            }

            pending.delete(sessionID as string);

            const done = Date.now();

            await ingest({
                id: stableId("prompt", "end", sessionID as string, record.messageID),
                kind: "prompt.end",
                timestamp: new Date(done).toISOString(),
                actor: opts.actor,
                task: record.task,
                title: `done: ${record.title}`,
                duration_ms: done - record.start,
                tags: ["opencode", "prompt"],
                data: {
                    sessionID,
                    messageID: record.messageID,
                    status: event.type === "session.error" ? "error" : "completed",
                },
                program: programEvents(),
            });
        },
        dispose: async () => {
            if (interval !== undefined) {
                clearInterval(interval);
            }

            await post("/api/programs/end", { program_id: programId }, 3);
        },
    };
};

export default OpenCodeActivity;