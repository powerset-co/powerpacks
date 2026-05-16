export function toJsonSafe(value: unknown): unknown {
  if (value == null) return value;
  const t = typeof value;
  if (t === "string" || t === "boolean") return value;
  if (t === "number") return Number.isFinite(value as number) ? value : null;
  if (t === "bigint") {
    const n = value as bigint;
    return n <= BigInt(Number.MAX_SAFE_INTEGER) && n >= BigInt(Number.MIN_SAFE_INTEGER) ? Number(n) : n.toString();
  }
  if (value instanceof Date) return value.toISOString();
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    const buffer = Buffer.from(value as Uint8Array);
    const text = buffer.toString("utf8");
    return text.includes("\uFFFD") ? buffer.toString("hex") : text;
  }
  if (Array.isArray(value)) return value.map(toJsonSafe);
  if (typeof value === "object") {
    const maybe = value as any;
    if (typeof maybe.toISOString === "function") return maybe.toISOString();
    if (maybe.constructor?.name && /Decimal|BigInt/i.test(maybe.constructor.name) && typeof maybe.toString === "function") {
      const text = maybe.toString();
      const num = Number(text);
      return Number.isSafeInteger(num) || (Number.isFinite(num) && Math.abs(num) < Number.MAX_SAFE_INTEGER) ? num : text;
    }
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(maybe)) out[k] = toJsonSafe(v);
    return out;
  }
  return String(value);
}

export function sendJson(res: any, data: unknown, status = 200) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(toJsonSafe(data)));
}
