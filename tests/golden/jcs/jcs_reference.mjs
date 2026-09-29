// Independent RFC 8785 reference for cross-language checks. ES2015+ JSON.stringify number and string
// serialization is exactly what JCS specifies; keys are sorted by UTF-16 code units (JS default sort).
// Reads JSON lines on stdin, writes the canonical form of each on stdout.
import { createInterface } from "node:readline";

function canonicalize(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canonicalize).join(",") + "]";
  return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonicalize(v[k])).join(",") + "}";
}

const rl = createInterface({ input: process.stdin });
for await (const line of rl) process.stdout.write(canonicalize(JSON.parse(line)) + "\n");
