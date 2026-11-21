// Receives one opt-in usage report from ./edit or ./reel, checks its shape and size,
// stores it in local_editor_runs, and pings Telegram on a tester's first run or a crash.
// {"summary": true} sends the weekly roll-up instead (at most once every 6 days).
// Secrets (set in the Supabase dashboard, never in this repo):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are built in.
import { createClient } from "jsr:@supabase/supabase-js@2";

const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const MAX_BYTES = 16_000;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function telegram(text: string): Promise<string> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
  const chat = Deno.env.get("TELEGRAM_CHAT_ID");
  if (!token || !chat) return "no-secrets";
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, text: text.slice(0, 3500) }),
      signal: AbortSignal.timeout(5000),
    });
    return r.ok ? "sent" : `http-${r.status}`;
  } catch {
    return "failed";
  }
}

const str = (v: unknown, max: number) => (typeof v === "string" ? v.slice(0, max) : null);
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && Math.abs(v) < 1e7 ? v : null);
const int = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && Math.abs(v) < 1e6 ? v : null);
const strs = (v: unknown, n: number, max: number) =>
  Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, n).map((x: string) => x.slice(0, max)) : [];

function validate(p: Record<string, unknown>): Record<string, unknown> | string {
  const uuid = /^[0-9a-f-]{36}$/i;
  if (typeof p.run_id !== "string" || !uuid.test(p.run_id)) return "run_id";
  const tester = str(p.tester, 40)?.trim();
  if (!tester || !/^[\w .-]+$/.test(tester)) return "tester";
  if (p.tool !== "edit" && p.tool !== "reel") return "tool";
  if (typeof p.ok !== "boolean") return "ok";
  const stages: Record<string, number> = {};
  if (p.stages && typeof p.stages === "object" && !Array.isArray(p.stages)) {
    for (const [k, v] of Object.entries(p.stages).slice(0, 30)) {
      if (/^[a-z0-9_]{1,30}$/.test(k) && num(v) !== null) stages[k] = v as number;
    }
  }
  const ts = str(p.ts, 40);
  return {
    run_id: p.run_id, tester, tool: p.tool, ok: p.ok,
    ts: ts && !isNaN(Date.parse(ts)) ? ts : null,
    version: str(p.version, 40), os: str(p.os, 80), python: str(p.python, 20),
    flags: strs(p.flags, 60, 40).filter((f) => /^--[\w-]+$/.test(f)),
    input_duration_s: num(p.input_duration_s), input_width: int(p.input_width),
    input_height: int(p.input_height), output_duration_s: num(p.output_duration_s),
    clips: int(p.clips), stages, total_s: num(p.total_s), exit_code: int(p.exit_code),
    error: str(p.error, 400), traceback: strs(p.traceback, 8, 200),
  };
}

async function summary(): Promise<Response> {
  const since = new Date(Date.now() - 6 * 86400_000).toISOString();
  const { data: recent } = await db.from("local_editor_summaries").select("id").gte("sent_at", since).limit(1);
  if (recent && recent.length) return json({ ok: true, skipped: "already sent this week" });
  const week = new Date(Date.now() - 7 * 86400_000).toISOString();
  const { data: rows, error } = await db.from("local_editor_runs")
    .select("tester, ok, total_s").gte("received_at", week).limit(5000);
  if (error) return json({ ok: false, error: "query" }, 500);
  const by: Record<string, { runs: number; fails: number; secs: number }> = {};
  for (const r of rows ?? []) {
    const t = (by[r.tester] ??= { runs: 0, fails: 0, secs: 0 });
    t.runs++; if (!r.ok) t.fails++; t.secs += r.total_s ?? 0;
  }
  const n = rows?.length ?? 0;
  const avg = n ? (rows!.reduce((a, r) => a + (r.total_s ?? 0), 0) / n).toFixed(0) : "0";
  const lines = Object.entries(by).sort((a, b) => b[1].runs - a[1].runs)
    .map(([k, v]) => `${k}: ${v.runs} runs, ${v.fails} failed, avg ${(v.secs / v.runs).toFixed(0)}s`);
  const body = `Local editor, last 7 days\n${n} runs, average ${avg}s\n` + (lines.join("\n") || "no runs");
  const tg = await telegram(body);
  if (tg === "sent") await db.from("local_editor_summaries").insert({ body });
  return json({ ok: true, telegram: tg, runs: n });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ ok: false, error: "POST only" }, 405);
  const raw = await req.text();
  if (raw.length > MAX_BYTES) return json({ ok: false, error: "too large" }, 413);
  let p: Record<string, unknown>;
  try { p = JSON.parse(raw); } catch { return json({ ok: false, error: "bad json" }, 400); }
  if (!p || typeof p !== "object" || Array.isArray(p)) return json({ ok: false, error: "bad json" }, 400);
  if (p.summary === true) return summary();

  const row = validate(p);
  if (typeof row === "string") return json({ ok: false, error: `bad field: ${row}` }, 400);

  // Light abuse guard: 200 reports per tester per hour.
  const hour = new Date(Date.now() - 3600_000).toISOString();
  const { count: lastHour } = await db.from("local_editor_runs").select("id", { count: "exact", head: true })
    .eq("tester", row.tester).gte("received_at", hour);
  if ((lastHour ?? 0) >= 200) return json({ ok: false, error: "rate limited" }, 429);

  const { count: before } = await db.from("local_editor_runs").select("id", { count: "exact", head: true })
    .eq("tester", row.tester);
  const { error } = await db.from("local_editor_runs").insert(row);
  if (error) {
    if (error.code === "23505") return json({ ok: true, duplicate: true });
    return json({ ok: false, error: "insert" }, 500);
  }

  let telegramStatus = "not-needed";
  const where = `${row.tool} ${row.version ?? "?"} on ${row.os ?? "?"}`;
  if (!row.ok && row.error) {
    telegramStatus = await telegram(
      `Local editor crash: ${row.tester}\n${where}\n${row.error}\n` + (row.traceback as string[]).join("\n"));
  } else if ((before ?? 0) === 0) {
    telegramStatus = await telegram(
      `Local editor: first run from ${row.tester}\n${where}, ${row.ok ? "worked" : "failed"} in ${row.total_s ?? "?"}s`);
  }
  return json({ ok: true, first_run: (before ?? 0) === 0, telegram: telegramStatus });
});
