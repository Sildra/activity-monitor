import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { request as httpRequest, type IncomingMessage } from "node:http";
import { request as httpsRequest } from "node:https";
import { hostname } from "node:os";
import { Plugin } from "@opencode/plugin";

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

const postJson = (url: string, body: string, timeoutMs: number): Promise<{ ok: boolean; status: number }> =>
    new Promise((resolve, reject) => {
        const target = new URL(url);
        const base = {
            method: "POST",
            headers: { "content-type": "application/json", "content-length": Buffer.byteLength(body) },
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

export default Plugin.define({
    id: "activity-monitor",
    async setup(ctx) {
        const port = envVar("ACTIVITY_MONITOR_PORT") ?? "8001";

        const opts: Options = {
            baseUrl: envVar("ACTIVITY_MONITOR_URL") ?? envVar("ACTIVITY_MONITOR_BASE_URL") ?? `http://localhost:${port}`,
            source: envVar("ACTIVITY_MONITOR_SOURCE") ?? "opencode",
            actor: defaultActor(),
            truncate: 120,
            heartbeatMs: 60_000,
            ...(ctx.options ?? {}),
        };
        const source = opts.source ?? "opencode";
        const programId = opts.programId ?? getPid();
        const host = hostname();
        const directory = ctx.location.directory;
        const pending = new Map<string, Pending>();
        const sessionTasks = new Map<string, string>();
        const shadowSessions = new Set<string>();
        const shadowTurns = new Set<string>();
        const permissionPending = new Map<string, PermissionPending>();
        const shadowAll = envShadow();

        let lastError = "";

        const report = (message: string): void => {
            if (!message || message === lastError) {
                return;
            }

            lastError = message;
            console.warn(`[activity-monitor] ${message}`);
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
            post(`/api/ingest/${encodeURIComponent(source)}`, event);

        const beat = (): Promise<boolean> => post("/api/programs/beat", { program_id: programId });

        const programEvents = (): { id: string; source: string; host: string } => ({ id: programId, source, host });

        await post(
            "/api/programs/start",
            { program_id: programId, source, host, actor: opts.actor },
            8,
            1000,
        );

        const interval = setInterval(() => {
            void beat();
        }, opts.heartbeatMs ?? 60_000);

        // V2 has no `session.error` event (it was a V1 legacy type): the idle event is
        // the completion signal, so a finished prompt always reports status "completed".
        const controller = new AbortController();

        void (async () => {
            try {
                for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
                    if (event.type === "permission.asked") {
                        const data = event.data;
                        const start = Date.now();

                        permissionPending.set(data.id, {
                            sessionID: data.sessionID,
                            start,
                            action: data.action,
                        });

                        if (shadowTurns.has(data.sessionID)) {
                            continue;
                        }

                        await ingest({
                            id: stableId("permission", "asked", data.id),
                            kind: "permission.asked",
                            timestamp: new Date(start).toISOString(),
                            actor: opts.actor,
                            task: sessionTask(data.sessionID),
                            title: `permission: ${data.action}`,
                            detail: data.resources.join(", "),
                            tags: ["opencode", "permission"],
                            data: {
                                sessionID: data.sessionID,
                                requestID: data.id,
                                action: data.action,
                                resources: data.resources,
                                source: data.source,
                            },
                            program: programEvents(),
                        });

                        continue;
                    }

                    if (event.type === "permission.replied") {
                        const data = event.data;
                        const record = permissionPending.get(data.requestID);
                        const denied = data.reply === "reject";

                        permissionPending.delete(data.requestID);

                        if (shadowTurns.has(data.sessionID)) {
                            continue;
                        }

                        const done = Date.now();

                        await ingest({
                            id: stableId("permission", "replied", data.requestID),
                            kind: "permission.replied",
                            timestamp: new Date(done).toISOString(),
                            actor: opts.actor,
                            task: sessionTask(data.sessionID),
                            title: `${denied ? "denied" : "approved"}: ${record?.action ?? "permission"}`,
                            duration_ms: record !== undefined ? done - record.start : null,
                            tags: ["opencode", "permission"],
                            data: {
                                sessionID: data.sessionID,
                                requestID: data.requestID,
                                reply: data.reply,
                                status: denied ? "denied" : "approved",
                                action: record?.action ?? null,
                            },
                            program: programEvents(),
                        });

                        continue;
                    }

                    if (event.type !== "session.idle") {
                        continue;
                    }

                    const sessionID = event.data.sessionID;

                    shadowTurns.delete(sessionID);

                    for (const [requestID, permission] of permissionPending) {
                        if (permission.sessionID === sessionID) {
                            permissionPending.delete(requestID);
                        }
                    }

                    const record = pending.get(sessionID);

                    if (record === undefined) {
                        continue;
                    }

                    pending.delete(sessionID);

                    const done = Date.now();

                    await ingest({
                        id: stableId("prompt", "end", sessionID, record.messageID),
                        kind: "prompt.end",
                        timestamp: new Date(done).toISOString(),
                        actor: opts.actor,
                        task: record.task,
                        title: `done: ${record.title}`,
                        duration_ms: done - record.start,
                        tags: ["opencode", "prompt"],
                        data: { sessionID, messageID: record.messageID, status: "completed" },
                        program: programEvents(),
                    });
                }
            } catch (error) {
                report(`event stream failed: ${(error as Error).message}`);
            }
        })();

        await ctx.session.hook("prompt", async (event) => {
            // `/shadow <prompt>` (or the global ACTIVITY_MONITOR_SHADOW) runs the prompt
            // normally but is never reported. The prefix is stripped so the model still
            // receives the real text when the command executor did not already strip it.
            const prefix = /^\s*\/shadow\b\s*/i.exec(event.prompt.text);

            if (prefix) {
                event.prompt.text = event.prompt.text.slice(prefix[0].length);
            }

            const text = event.prompt.text.trim();
            const turnShadowed = shadowSessions.delete(event.sessionID) || prefix !== null;

            if (turnShadowed) {
                shadowTurns.add(event.sessionID);
            }

            if (!text || shadowAll || turnShadowed) {
                return;
            }

            let agent: string | undefined;
            let model: string | undefined;

            try {
                const session = await ctx.session.get({ sessionID: event.sessionID });
                agent = session.agent;
                model = session.model !== undefined ? `${session.model.providerID}/${session.model.id}` : undefined;
            } catch {
                // Best-effort enrichment; the prompt still goes through.
            }

            const title = firstTask(text, opts.truncate ?? 120);

            pending.set(event.sessionID, {
                messageID: event.messageID,
                title,
                task: sessionTask(event.sessionID),
                start: Date.now(),
            });

            await ingest({
                id: stableId("prompt", "begin", event.sessionID, event.messageID),
                kind: "prompt.begin",
                timestamp: new Date().toISOString(),
                actor: opts.actor,
                task: sessionTask(event.sessionID),
                title: `prompt: ${title}`,
                detail: text,
                tags: ["opencode", "prompt"],
                data: {
                    sessionID: event.sessionID,
                    messageID: event.messageID,
                    agent,
                    model,
                    directory,
                },
                program: programEvents(),
            });
        });

        // /task is registered as a real command whose executor does not submit a
        // prompt, so it pins the session's task without producing an assistant turn
        // (the V1 plugin achieved the same by intercepting and aborting).
        await ctx.command.transform((editor) => {
            editor.add({
                name: "task",
                description: "Set the Activity Monitor task id (e.g. a Jira key) for this session",
                execute: async (input) => {
                    const task = input.prompt.text
                        .replace(/^\s*\/task\b\s*/i, "")
                        .replace(/^Set the Activity Monitor task to:\s*/i, "")
                        .replace(/^["']|["']$/g, "")
                        .trim();

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
                },
            });

            // /shadow <prompt> submits the prompt normally but marks the session so the
            // prompt hook skips reporting for that turn (a per-turn ACTIVITY_MONITOR_SHADOW).
            editor.add({
                name: "shadow",
                description: "Send a prompt without reporting it to the Activity Monitor",
                execute: async (input) => {
                    const text = input.prompt.text.replace(/^\s*\/shadow\b\s*/i, "").trim();

                    if (!text) {
                        return;
                    }

                    shadowSessions.add(input.sessionID);

                    // Safety net: if the prompt hook never consumes the marker, drop it so a
                    // later prompt in this session is not silently shadowed.
                    const cleanup = setTimeout(() => shadowSessions.delete(input.sessionID), 30_000);
                    cleanup.unref?.();

                    try {
                        await ctx.session.prompt({ sessionID: input.sessionID, text });
                    } catch (error) {
                        shadowSessions.delete(input.sessionID);
                        report(`shadow prompt failed: ${(error as Error).message}`);
                    }
                },
            });
        });

        return () => {
            clearInterval(interval);
            controller.abort();
            void post("/api/programs/end", { program_id: programId }, 3);
        };
    },
});