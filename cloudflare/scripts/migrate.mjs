#!/usr/bin/env node
// Safe-to-repeat version of migrations/0002_story.sql.
//
//   node scripts/migrate.mjs --local  [--persist-to DIR]
//   node scripts/migrate.mjs --remote            (the lead runs this one; it is never run from the test scripts)
//
// SQLite/D1 cannot "ADD COLUMN IF NOT EXISTS", so each ALTER TABLE ... ADD COLUMN in the migration file is skipped when the
// column is already there; every other statement in the file is written with IF NOT EXISTS and simply runs.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const mode = args.includes("--remote") ? "--remote" : "--local";
const persist = args.includes("--persist-to") ? ["--persist-to", args[args.indexOf("--persist-to") + 1]] : [];
const file = args.includes("--file") ? args[args.indexOf("--file") + 1] : "migrations/0002_story.sql";
const DB = "signal2care";

function d1(sql) {
  const out = execFileSync("npx", ["wrangler", "d1", "execute", DB, mode, ...persist, "--json", "--command", sql], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] });
  const start = out.indexOf("[");
  return start < 0 ? [] : JSON.parse(out.slice(start));
}

const columns = (table) => new Set((d1(`SELECT name FROM pragma_table_info('${table}')`)[0]?.results || []).map((r) => r.name));

const sql = readFileSync(join(root, file), "utf8").split("\n").filter((l) => !l.trim().startsWith("--")).join("\n");
const statements = sql.split(";").map((s) => s.trim()).filter(Boolean);
const have = new Map();
let applied = 0, skipped = 0;
for (const stmt of statements) {
  const alter = /^ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)/i.exec(stmt);
  if (alter) {
    const [, table, column] = alter;
    if (!have.has(table)) have.set(table, columns(table));
    if (have.get(table).has(column)) { console.log(`skip   ${table}.${column} (already there)`); skipped++; continue; }
  }
  d1(stmt);
  console.log(`apply  ${stmt.replace(/\s+/g, " ").slice(0, 90)}`);
  applied++;
}
console.log(`done: ${applied} applied, ${skipped} skipped (${mode.slice(2)})`);
