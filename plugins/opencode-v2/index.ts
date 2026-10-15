import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { request as httpRequest, type IncomingMessage } from "node:http";
import { request as httpsRequest } from "node:https";
import { hostname } from "node:os";
import { Plugin } from "@opencode/plugin";
import { ActivityMonitor } from "./rpc.js";

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
    task: string | undefined;
    start: number;
};

type PermissionPending = {
    sessionID: string;
    start: number;
    action: string;
};

type QuestionPending = {
    sessionID: string;
    start: number;
    title: string;
};

type QuestionField = {
    key?: string;
    title?: string;
    description?: string;
    type?: string;
    options?: Array<{ value?: string; label?: string; description?: string }>;
};

type ConnectionState = "pending" | "ok" | "error";

// The `question` tool renders each question as a form field (see opencode's
// QuestionTool): the field `description` is the question, `title` is the short
// header, and `options` are the choices. Forms created for other reasons (e.g.
// integration auth) carry a different `metadata.kind`, so we can tell them apart.
const questionFields = (form: { fields?: unknown }): QuestionField[] =>
    Array.isArray(form.fields) ? (form.fields as QuestionField[]) : [];

const formatAnswer = (answer: unknown): string | null => {
    if (answer === undefined || answer === null) {
        return null;
    }

    if (Array.isArray(answer)) {
        return answer.length > 0 ? answer.map((part) => String(part)).join(", ") : null;
    }

    const text = String(answer);

    return text.length > 0 ? text : null;
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
    envVar("ACTIVITY_MONITOR_DISPLAY_USER") ??
    envVar("ACTIVITY_MONITOR_ACTOR") ??
    envVar("USER") ??
    envVar("USERNAME") ??
    "opencode";

const envShadow = (): boolean =>
    ["1", "true", "yes", "on"].includes((envVar("ACTIVITY_MONITOR_SHADOW") ?? "").toLowerCase());

const stableId = (...parts: string[]): string => createHash("sha1").update(parts.join("\u001f")).digest("hex");

const firstTask = (text: string, max: number): string => {
    const line = text.split("\n").map((part) => part.trim()).find(Boolean) ?? text.trim();

    return line.length > max ? `${line.slice(0, max)}\u2026` : line;
};

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
        const host = envVar("ACTIVITY_MONITOR_ORIGIN") ?? hostname();
        const directory = ctx.location.directory;
        const pending = new Map<string, Pending>();
        const sessionTasks = new Map<string, string>();
        const shadowSessions = new Set<string>();
        const shadowTurns = new Set<string>();
        const permissionPending = new Map<string, PermissionPending>();
        const questionPending = new Map<string, QuestionPending>();
        const shadowAll = envShadow();

        // The state surfaced to the TUI footer. `shadow` is derived below rather
        // than stored, so a global or per-turn shadow always wins over the last
        // request outcome.
        const connection = { state: "pending" as ConnectionState, message: "" };
        let emitState: ((data: { state: string; message: string }) => void) | undefined;
        let emitTask: ((data: { sessionID: string; task: string }) => void) | undefined;
        let publishedState = "";

        const currentState = (): { state: string; message: string } =>
            shadowAll || shadowTurns.size > 0
                ? { state: "shadow", message: "" }
                : { state: connection.state, message: connection.message };

        const publishState = (): void => {
            const next = currentState();
            const key = `${next.state}\u001f${next.message}`;

            if (key === publishedState) {
                return;
            }

            publishedState = key;
            emitState?.(next);
        };

        const setConnection = (state: ConnectionState, message: string): void => {
            connection.state = state;
            connection.message = message;
            publishState();
        };

        let lastError = "";

        const report = (message: string): void => {
            if (!message || message === lastError) {
                return;
            }

            lastError = message;
            console.warn(`[activity-monitor] ${message}`);
        };

        // Expose the state and the per-session task to the TUI plugin over the
        // shared RPC contract so the OpenCode sidebar can render them (see
        // tui.tsx). Best effort: older builds without the RPC domain must not
        // break the rest of the plugin.
        try {
            const registration = await ctx.rpc.register(ActivityMonitor, {
                status: async () => currentState(),
                task: async (input) => {
                    const data = (input ?? {}) as Record<string, unknown>;
                    const sessionID = typeof data.sessionID === "string" ? data.sessionID : "";

                    return { task: sessionTasks.get(sessionID) ?? "" };
                },
            });

            emitState = (data) => {
                void registration.events.emit("state", data);
            };

            emitTask = (data) => {
                void registration.events.emit("task", data);
            };
        } catch (error) {
            report(`rpc registration failed: ${(error as Error).message}`);
        }

        publishState();

        const sessionTask = (sessionID: string | undefined): string | undefined =>
            sessionID !== undefined ? sessionTasks.get(sessionID) : undefined;

        // `on_status` hands the HTTP status of a rejected reply back to the caller
        // so it can tell a meaningful answer (404 = the backend has no such
        // program) from a plain failure to report.
        const post = async (
            path: string,
            body: unknown,
            attempts = 1,
            backoffMs = 750,
            on_status?: (status: number) => void,
        ): Promise<boolean> => {
            if (shadowAll) {
                return true;
            }

            let failure = "";

            for (let attempt = 0; attempt < attempts; attempt += 1) {
                try {
                    const response = await postJson(`${opts.baseUrl}${path}`, JSON.stringify(body), 3000);

                    if (response.ok) {
                        lastError = "";
                        setConnection("ok", "");

                        return true;
                    }

                    failure = `${path} rejected (${response.status})`;
                    on_status?.(response.status);
                } catch (error) {
                    failure = `${path} failed: ${(error as Error).message}`;
                }

                await new Promise((resolve) => setTimeout(resolve, backoffMs));
            }

            report(failure);
            setConnection("error", failure);

            return false;
        };

        const ingest = (event: Record<string, unknown>): Promise<boolean> =>
            post(`/api/ingest/${encodeURIComponent(source)}`, { ...event, host });

        const register = (): Promise<boolean> =>
            post("/api/programs/start", { program_id: programId, source, host, actor: opts.actor }, 8, 1000);

        const beat = async (): Promise<boolean> => {
            let missing = false;

            const sent = await post("/api/programs/beat", { program_id: programId }, 1, 750, (status) => {
                missing = status === 404;
            });

            if (sent || !missing) {
                return sent;
            }

            // The backend answered, but has no active program for this id: the
            // first `start` ran while it was down, or the row was expired. Re-run
            // it so presence recovers instead of 404ing on every beat. A failed
            // beat that never reached the backend (network error) is not 404 and
            // is left alone — that one just gets reported.
            await register();

            return post("/api/programs/beat", { program_id: programId }, 1);
        };

        const programEvents = (): { id: string; source: string; host: string } => ({ id: programId, source, host });

        await register();

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

                    if (event.type === "form.created") {
                        const form = event.data.form;
                        const metadata = form.metadata as { kind?: string } | undefined;

                        // Only the built-in `question` tool tags its form with kind "question";
                        // unrelated forms (integration auth, etc.) are ignored.
                        if (metadata?.kind !== "question") {
                            continue;
                        }

                        const start = Date.now();
                        const fields = questionFields(form);
                        const first = fields[0];
                        const header = first?.title ?? form.title;
                        const question = first?.description ?? first?.title ?? form.title;
                        const options = (first?.options ?? [])
                            .map((option) => option.label ?? option.value)
                            .filter((value): value is string => typeof value === "string");

                        questionPending.set(form.id, { sessionID: form.sessionID, start, title: header });

                        if (shadowTurns.has(form.sessionID)) {
                            continue;
                        }

                        await ingest({
                            id: stableId("question", "asked", form.id),
                            kind: "question.asked",
                            timestamp: new Date(start).toISOString(),
                            actor: opts.actor,
                            task: sessionTask(form.sessionID),
                            title: `question: ${header}`,
                            detail: question,
                            tags: ["opencode", "question"],
                            data: {
                                sessionID: form.sessionID,
                                requestID: form.id,
                                header,
                                question,
                                options,
                            },
                            program: programEvents(),
                        });

                        continue;
                    }

                    if (event.type === "form.replied" || event.type === "form.cancelled") {
                        const data = event.data;
                        const record = questionPending.get(data.id);

                        // A reply for a form we never saw (or that was not a question) is not ours.
                        if (record === undefined) {
                            continue;
                        }

                        questionPending.delete(data.id);

                        if (shadowTurns.has(data.sessionID)) {
                            continue;
                        }

                        const done = Date.now();
                        const dismissed = event.type === "form.cancelled";
                        const answers =
                            event.type === "form.replied"
                                ? Object.values(event.data.answer as Record<string, unknown>)
                                      .map(formatAnswer)
                                      .filter((value): value is string => value !== null)
                                : [];
                        const answer = answers.length > 0 ? answers.join(" \u00b7 ") : null;

                        await ingest({
                            id: stableId("question", "replied", data.id),
                            kind: "question.replied",
                            timestamp: new Date(done).toISOString(),
                            actor: opts.actor,
                            task: sessionTask(data.sessionID),
                            title: `${dismissed ? "dismissed" : "answered"}: ${record.title}`,
                            detail: answer,
                            duration_ms: done - record.start,
                            tags: ["opencode", "question"],
                            data: {
                                sessionID: data.sessionID,
                                requestID: data.id,
                                status: dismissed ? "dismissed" : "answered",
                                answer,
                            },
                            program: programEvents(),
                        });

                        continue;
                    }

                    if (event.type !== "session.idle") {
                        continue;
                    }

                    const sessionID = event.data.sessionID;

                    if (shadowTurns.delete(sessionID)) {
                        publishState();
                    }

                    for (const [requestID, permission] of permissionPending) {
                        if (permission.sessionID === sessionID) {
                            permissionPending.delete(requestID);
                        }
                    }

                    for (const [formID, question] of questionPending) {
                        if (question.sessionID === sessionID) {
                            questionPending.delete(formID);
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
                publishState();
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
                    const previous = sessionTask(input.sessionID) ?? "";

                    if (task) {
                        sessionTasks.set(input.sessionID, task);
                    } else {
                        sessionTasks.delete(input.sessionID);
                    }

                    if (task !== previous) {
                        emitTask?.({ sessionID: input.sessionID, task });
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