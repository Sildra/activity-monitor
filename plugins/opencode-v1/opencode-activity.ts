import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { request as httpRequest, type IncomingMessage } from "node:http";
import { request as httpsRequest } from "node:https";
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

type PermissionPending = {
    sessionID: string;
    start: number;
    action: string;
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

const envShadow = (): boolean =>
    ["1", "true", "yes", "on"].includes((envVar("ACTIVITY_MONITOR_SHADOW") ?? "").toLowerCase());

const stableId = (...parts: string[]): string => createHash("sha1").update(parts.join("\u001f")).digest("hex");

const firstTask = (text: string, max: number): string => {
    const line = text.split("\n").map((part) => part.trim()).find(Boolean) ?? text.trim();

    return line.length > max ? `${line.slice(0, max)}\u2026` : line;
};

const projectName = (directory: string): string => directory.split(/[\\/]/).filter(Boolean).pop() ?? "opencode";

type TlsOptions = { ca?: Buffer; cert?: Buffer; key?: Buffer; rejectUnauthorized?: boolean };

const readFile = (path: string | undefined): Buffer | undefined =>
    path !== undefined && path !== "" ? readFileSync(path) : undefined;

let cachedTls: TlsOptions | undefined;

const tlsOptions = (): TlsOptions => {
    if (cachedTls !== undefined) {
        return cachedTls;
    }

    const options: TlsOptions = {};
    const ca = readFile(envVar("ACTIVITY_MONITOR_CA_CERT"));
    const cert = readFile(envVar("ACTIVITY_MONITOR_CLIENT_CERT"));
    const key = readFile(envVar("ACTIVITY_MONITOR_CLIENT_KEY"));
    const insecure = (envVar("ACTIVITY_MONITOR_SSL_INSECURE") ?? envVar("ACTIVITY_MONITOR_INSECURE") ?? "").toLowerCase();

    if (ca !== undefined) {
        options.ca = ca;
    }

    if (cert !== undefined) {
        options.cert = cert;
    }

    if (key !== undefined) {
        options.key = key;
    }

    if (["1", "true", "yes", "on"].includes(insecure)) {
        options.rejectUnauthorized = false;
    }

    cachedTls = options;

    return options;
};

const authHeaders = (): Record<string, string> => {
    const username = envVar("ACTIVITY_MONITOR_USERNAME");

    if (username === undefined || username === "") {
        return {};
    }

    const password = envVar("ACTIVITY_MONITOR_PASSWORD") ?? "";

    return { authorization: `Basic ${Buffer.from(`${username}:${password}`, "utf8").toString("base64")}` };
};

const postJson = (url: string, body: string, timeoutMs: number): Promise<{ ok: boolean; status: number }> =>
    new Promise((resolve, reject) => {
        const target = new URL(url);
        const base = {
            method: "POST",
            headers: {
                "content-type": "application/json",
                "content-length": Buffer.byteLength(body),
                ...authHeaders(),
            },
            timeout: timeoutMs,
        };
        const onResponse = (res: IncomingMessage): void => {
            const status = res.statusCode ?? 0;

            res.resume();
            res.on("end", () => resolve({ ok: status >= 200 && status < 300, status }));
        };

        const req =
            target.protocol === "https:"
                ? httpsRequest(target, { ...base, ...tlsOptions() }, onResponse)
                : httpRequest(target, base, onResponse);

        req.on("timeout", () => req.destroy(new Error("request timed out")));
        req.on("error", reject);
        req.end(body);
    });

export const OpenCodeActivity: Plugin = async ({ directory, client }, options) => {
    const port = envVar("ACTIVITY_MONITOR_PORT") ?? "8001";

    const opts: Options = {
        baseUrl: envVar("ACTIVITY_MONITOR_URL") ?? envVar("ACTIVITY_MONITOR_BASE_URL") ?? `http://localhost:${port}`,
        source: envVar("ACTIVITY_MONITOR_SOURCE") ?? "opencode",
        actor: defaultActor(),
        truncate: 120,
        heartbeatMs: 60_000,
        ...(options ?? {}),
    };
    const source = opts.source ?? "opencode";
    const programId = opts.programId ?? getPid();
    const host = envVar("ACTIVITY_MONITOR_ORIGIN") ?? hostname();
    const pending = new Map<string, Pending>();
    const sessionTasks = new Map<string, string>();
    const shadowSessions = new Set<string>();
    const shadowTurns = new Set<string>();
    const permissionPending = new Map<string, PermissionPending>();
    const shadowAll = envShadow();

    let lastError = "";

    const toast = (message: string, variant: "info" | "warning" | "error"): void => {
        void client.tui
            .showToast({ body: { title: "Activity Monitor", message, variant, duration: 6000 } })
            .catch(() => undefined);
    };

    const report = (message: string): void => {
        if (!message || message === lastError) {
            return;
        }

        lastError = message;
        toast(message, "error");
    };

    const sessionTask = (sessionID: string | undefined): string =>
        (sessionID !== undefined ? sessionTasks.get(sessionID) : undefined) ?? projectName(directory);

    const post = async (path: string, body: unknown, attempts = 1, backoffMs = 750): Promise<boolean> => {
        if (shadowAll) {
            return true;
        }

        let failure = "";

        for (let attempt = 0; attempt < attempts; attempt += 1) {
            try {
                const response = await postJson(`${opts.baseUrl}${path}`, JSON.stringify(body), 3000);

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
        post(`/api/ingest/${encodeURIComponent(source)}`, { ...event, host });

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
        "command.execute.before": async (input) => {
            const command = input.command.replace(/^\//, "");

            // /shadow <prompt>: run the prompt but mark the session so chat.message
            // skips reporting for that turn (a per-turn ACTIVITY_MONITOR_SHADOW).
            // Unlike /task this does not throw, so the command proceeds normally.
            if (command === "shadow") {
                if (input.arguments.trim()) {
                    shadowSessions.add(input.sessionID);

                    // Safety net: if chat.message never consumes the marker, drop it so a
                    // later prompt in this session is not silently shadowed.
                    const cleanup = setTimeout(() => shadowSessions.delete(input.sessionID), 30_000);
                    cleanup.unref?.();
                }

                return;
            }

            if (command !== "task") {
                return;
            }

            const task = input.arguments.trim().replace(/^["']|["']$/g, "");

            if (task) {
                sessionTasks.set(input.sessionID, task);
            } else {
                sessionTasks.delete(input.sessionID);
            }

            await ingest({
                id: stableId("task", input.sessionID, task),
                kind: "task.set",
                timestamp: new Date().toISOString(),
                actor: opts.actor,
                task: task || null,
                title: task ? `task: ${task}` : "task cleared",
                tags: ["opencode", "task"],
                data: { sessionID: input.sessionID },
                program: programEvents(),
            });

            toast(task ? `Task set to ${task}` : "Task cleared", "info");

            // There is no cancellation API; throwing aborts the command so no prompt
            // (and no model turn) is sent for /task. The TUI ignores the abort.
            throw new Error("activity-monitor: task command handled");
        },
        "chat.message": async (input, output) => {
            // A `/shadow <prompt>` prefix (when it reaches the message hook) is stripped
            // so the model receives the real text; the turn is still not reported.
            const prefixPart = output.parts.find((part) => part.type === "text" && /^\s*\/shadow\b\s*/i.test(part.text));

            if (prefixPart !== undefined) {
                prefixPart.text = prefixPart.text.replace(/^\s*\/shadow\b\s*/i, "");
            }

            const text = output.parts
                .filter((part) => part.type === "text")
                .map((part) => part.text)
                .join("\n")
                .trim();

            if (!text) {
                return;
            }

            const turnShadowed = shadowSessions.delete(input.sessionID) || prefixPart !== undefined;

            if (turnShadowed) {
                shadowTurns.add(input.sessionID);
            }

            if (shadowAll || turnShadowed) {
                return;
            }

            const start = output.message.time?.created ?? Date.now();
            const title = firstTask(text, opts.truncate ?? 120);

            pending.set(output.message.sessionID, {
                messageID: output.message.id,
                title,
                task: sessionTask(output.message.sessionID),
                start,
            });

            await ingest({
                id: stableId("prompt", "begin", input.sessionID, output.message.id),
                kind: "prompt.begin",
                timestamp: new Date(start).toISOString(),
                actor: opts.actor,
                task: sessionTask(output.message.sessionID),
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
            if (event.type === "permission.updated") {
                const permission = event.properties;
                const start = Date.now();
                const resources = Array.isArray(permission.pattern)
                    ? permission.pattern
                    : permission.pattern !== undefined
                      ? [permission.pattern]
                      : [];

                permissionPending.set(permission.id, {
                    sessionID: permission.sessionID,
                    start,
                    action: permission.type,
                });

                if (shadowTurns.has(permission.sessionID)) {
                    return;
                }

                await ingest({
                    id: stableId("permission", "asked", permission.id),
                    kind: "permission.asked",
                    timestamp: new Date(start).toISOString(),
                    actor: opts.actor,
                    task: sessionTask(permission.sessionID),
                    title: permission.title || `permission: ${permission.type}`,
                    detail: resources.join(", "),
                    tags: ["opencode", "permission"],
                    data: {
                        sessionID: permission.sessionID,
                        requestID: permission.id,
                        action: permission.type,
                        resources,
                        messageID: permission.messageID,
                    },
                    program: programEvents(),
                });

                return;
            }

            if (event.type === "permission.replied") {
                const { sessionID, permissionID, response } = event.properties;
                const record = permissionPending.get(permissionID);
                const denied = response === "reject" || response === "deny" || response === "denied";

                permissionPending.delete(permissionID);

                if (shadowTurns.has(sessionID)) {
                    return;
                }

                const done = Date.now();

                await ingest({
                    id: stableId("permission", "replied", permissionID),
                    kind: "permission.replied",
                    timestamp: new Date(done).toISOString(),
                    actor: opts.actor,
                    task: sessionTask(sessionID),
                    title: `${denied ? "denied" : "approved"}: ${record?.action ?? "permission"}`,
                    duration_ms: record !== undefined ? done - record.start : null,
                    tags: ["opencode", "permission"],
                    data: {
                        sessionID,
                        requestID: permissionID,
                        reply: response,
                        status: denied ? "denied" : "approved",
                        action: record?.action ?? null,
                    },
                    program: programEvents(),
                });

                return;
            }

            if (event.type !== "session.idle" && event.type !== "session.error") {
                return;
            }

            const sessionID = event.properties.sessionID;

            if (sessionID !== undefined) {
                shadowTurns.delete(sessionID as string);

                for (const [permissionID, permission] of permissionPending) {
                    if (permission.sessionID === sessionID) {
                        permissionPending.delete(permissionID);
                    }
                }
            }

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