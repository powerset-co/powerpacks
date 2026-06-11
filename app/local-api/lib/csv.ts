import fs from "fs";
import { createGunzip } from "zlib";
import { createInterface } from "readline";

export type CsvDocument = { headers: string[]; rows: Record<string, string>[] };

export function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === "," && !inQuotes) {
      values.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  values.push(current);
  return values;
}

export function parseCsvDocument(text: string): CsvDocument {
  const records: string[][] = [];
  let row: string[] = [];
  let current = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === '"') {
      if (inQuotes && text[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === "," && !inQuotes) {
      row.push(current);
      current = "";
    } else if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      row.push(current);
      if (row.some((value) => value.length > 0)) records.push(row);
      row = [];
      current = "";
    } else {
      current += char;
    }
  }

  if (current.length > 0 || row.length > 0) {
    row.push(current);
    if (row.some((value) => value.length > 0)) records.push(row);
  }

  const headers = records[0] || [];
  const rows = records.slice(1).map((values) => {
    const out: Record<string, string> = {};
    headers.forEach((header, i) => {
      out[header] = values[i] ?? "";
    });
    return out;
  });
  return { headers, rows };
}

export async function readJsonlWindow(filePath: string, offset: number, limit: number): Promise<any[]> {
  if (!filePath || !fs.existsSync(filePath)) return [];
  const rows: any[] = [];
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  let index = 0;
  for await (const line of rl) {
    if (!line.trim()) continue;
    if (index >= offset && rows.length < limit) rows.push(JSON.parse(line));
    index += 1;
    if (rows.length >= limit) {
      rl.close();
      input.destroy();
      break;
    }
  }
  return rows;
}

export async function readJsonlForIds(filePath: string, ids: Set<string>): Promise<Record<string, any>> {
  const rows: Record<string, any> = {};
  if (!filePath || !fs.existsSync(filePath) || ids.size === 0) return rows;
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    const row = JSON.parse(line);
    const personId = String(row.person_id || "");
    if (ids.has(personId)) {
      rows[personId] = row;
      if (Object.keys(rows).length >= ids.size) {
        rl.close();
        input.destroy();
        break;
      }
    }
  }
  return rows;
}

export async function readCsvWindow(filePath: string, offset: number, limit: number): Promise<{ rows: any[]; total: number }> {
  if (!filePath || !fs.existsSync(filePath)) return { rows: [], total: 0 };
  const rows: any[] = [];
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });
  let headers: string[] | null = null;
  let index = 0;
  for await (const line of rl) {
    if (!headers) {
      headers = parseCsvLine(line);
      continue;
    }
    if (!line.trim()) continue;
    if (index >= offset && rows.length < limit) {
      const values = parseCsvLine(line);
      rows.push(Object.fromEntries(headers.map((header, i) => [header, values[i] ?? ""])));
    }
    index += 1;
  }
  return { rows, total: index };
}

export async function readProfilesForIds(filePath: string, ids: Set<string>, gzipped = false): Promise<Record<string, any>> {
  const profiles: Record<string, any> = {};
  if (!filePath || !fs.existsSync(filePath) || ids.size === 0) return profiles;

  const input = gzipped
    ? fs.createReadStream(filePath).pipe(createGunzip())
    : fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = createInterface({ input, crlfDelay: Infinity });

  for await (const line of rl) {
    if (!line.trim()) continue;
    const profile = JSON.parse(line);
    const personId = String(profile.person_id || "");
    if (ids.has(personId)) {
      profiles[personId] = profile;
      if (Object.keys(profiles).length >= ids.size) {
        rl.close();
        input.destroy();
        break;
      }
    }
  }
  return profiles;
}
