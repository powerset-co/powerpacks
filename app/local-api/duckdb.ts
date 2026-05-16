import fs from "fs";
import duckdb from "duckdb";
import { toJsonSafe } from "./jsonSafe";

export type DuckDbHandle = { db: duckdb.Database; conn: duckdb.Connection; path: string };

function all(conn: duckdb.Connection, sql: string, params: unknown[] = []): Promise<any[]> {
  return new Promise((resolve, reject) => {
    conn.all(sql, ...(params as any[]), (err: Error | null, rows: any[]) => err ? reject(err) : resolve(rows || []));
  });
}

export async function openConfiguredDuckDb(): Promise<DuckDbHandle | null> {
  const dbPath = process.env.POWERPACKS_LOCAL_SEARCH_DB;
  if (!dbPath || !fs.existsSync(dbPath) || !fs.statSync(dbPath).isFile()) return null;
  const db = new duckdb.Database(dbPath, { access_mode: "READ_ONLY" });
  const conn = db.connect();
  return { db, conn, path: dbPath };
}

export function closeDuckDb(handle: DuckDbHandle | null) {
  if (!handle) return;
  try { handle.conn.close(); } catch {}
  try { handle.db.close(); } catch {}
}

export async function listTables(handle: DuckDbHandle): Promise<Set<string>> {
  const rows = await all(handle.conn, "select table_name from information_schema.tables where table_schema not in ('pg_catalog','information_schema')");
  return new Set(rows.map((r) => String(r.table_name)));
}

export async function hasTable(handle: DuckDbHandle, table: string): Promise<boolean> {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(table)) return false;
  const rows = await all(handle.conn, "select 1 from information_schema.tables where table_name = ? limit 1", [table]);
  return rows.length > 0;
}

export async function tableColumns(handle: DuckDbHandle, table: string): Promise<string[]> {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(table)) return [];
  const rows = await all(handle.conn, "select column_name from information_schema.columns where table_name = ? order by ordinal_position", [table]);
  return rows.map((r) => String(r.column_name));
}

export async function queryRows(handle: DuckDbHandle, sql: string, params: unknown[] = []): Promise<any[]> {
  const rows = await all(handle.conn, sql, params);
  return toJsonSafe(rows) as any[];
}
