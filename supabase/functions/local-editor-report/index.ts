// Receives one usage report from ./edit or ./reel and stores it in local_editor_runs.
// No invite code, no row: a request must carry an invite Matt handed out, matched by its
// SHA-256 hash against local_editor_invites (constant-time). The code maps to the tester
// name server-side. Caps: 16 KB per request, 50 rows per code per 24 hours.
// No notifications of any kind. Matt reads the local_editor_runs_summary view when he wants.
import { createClient } from "jsr:@supabase/supabase-js@2";

const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const MAX_BYTES = 16_000;
const DAILY_CAP = 50;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function sha256hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameHex(a: string, b: string): boolean {
  // Constant time for equal-length hex digests: no early exit.
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const str = (v: unknown, max: number) => (typeof v === "string" ? v.slice(0, max) : null);
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && Math.abs(v) < 1e7 ? v : null);
const int = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && Math.abs(v) < 1e6 ? v : null);
const strs = (v: unknown, n: number, max: number) =>
  Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, n).map((x: string) => x.slice(0, max)) : [];

function validate(p: Record<string, unknown>): Record<string, unknown> | string {
  if (typeof p.run_id !== "string" || !/^[0-9a-f-]{36}$/i.test(p.run_id)) return "run_id";
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
    run_id: p.run_id, tool: p.tool, ok: p.ok,
    ts: ts && !isNaN(Date.parse(ts)) ? ts : null,
    version: str(p.version, 40), os: str(p.os, 80), python: str(p.python, 20),
    flags: strs(p.flags, 60, 40).filter((f) => /^--[\w-]+$/.test(f)),
    input_duration_s: num(p.input_duration_s), input_width: int(p.input_width),
    input_height: int(p.input_height), output_duration_s: num(p.output_duration_s),
    clips: int(p.clips), stages, total_s: num(p.total_s), exit_code: int(p.exit_code),
    error: str(p.error, 400), traceback: strs(p.traceback, 8, 200),
  };
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ ok: false, error: "POST only" }, 405);
  const raw = await req.text();
  if (raw.length > MAX_BYTES) return json({ ok: false, error: "too large" }, 413);
  let p: Record<string, unknown>;
  try { p = JSON.parse(raw); } catch { return json({ ok: false, error: "bad json" }, 400); }
  if (!p || typeof p !== "object" || Array.isArray(p)) return json({ ok: false, error: "bad json" }, 400);

  const code = typeof p.invite === "string" ? p.invite.trim() : "";
  if (!/^[A-Za-z0-9_-]{8,80}$/.test(code)) return json({ ok: false, error: "invite code required" }, 401);
  const hash = await sha256hex(code);
  const { data: invites, error: invErr } = await db.from("local_editor_invites")
    .select("id, code_hash, tester").eq("active", true).limit(500);
  if (invErr) return json({ ok: false, error: "lookup" }, 500);
  let invite: { id: number; tester: string } | null = null;
  for (const inv of invites ?? []) {
    if (sameHex(inv.code_hash, hash) && !invite) invite = inv;
  }
  if (!invite) return json({ ok: false, error: "invalid invite code" }, 401);

  const row = validate(p);
  if (typeof row === "string") return json({ ok: false, error: `bad field: ${row}` }, 400);

  const day = new Date(Date.now() - 86400_000).toISOString();
  const { count } = await db.from("local_editor_runs").select("id", { count: "exact", head: true })
    .eq("invite_id", invite.id).gte("received_at", day);
  if ((count ?? 0) >= DAILY_CAP) return json({ ok: false, error: "daily limit reached" }, 429);

  const { error } = await db.from("local_editor_runs")
    .insert({ ...row, tester: invite.tester, invite_id: invite.id });
  if (error) {
    if (error.code === "23505") return json({ ok: true, duplicate: true });
    return json({ ok: false, error: "insert" }, 500);
  }
  return json({ ok: true });
});
