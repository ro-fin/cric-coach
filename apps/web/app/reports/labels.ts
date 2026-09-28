/**
 * Readable names for metric keys (e.g. a goal's `control_pct`). Pure
 * formatting of the key itself — no meaning is invented: underscores become
 * spaces, a trailing unit suffix becomes the unit, and the first letter is
 * capitalised. An unknown shape passes through unchanged apart from that.
 */

const UNIT_SUFFIXES: Record<string, string> = {
  pct: "%",
  cm: "cm",
  mm: "mm",
  m: "m",
  kph: "km/h",
  deg: "°",
  ms: "ms",
  s: "s",
};

export function metricLabel(key: string): string {
  const parts = key.split("_").filter((part) => part !== "");
  if (parts.length === 0) {
    return key;
  }
  const last = parts[parts.length - 1];
  const unit = parts.length > 1 ? UNIT_SUFFIXES[last] : undefined;
  const words = (unit === undefined ? parts : parts.slice(0, -1)).join(" ");
  const name = words.charAt(0).toUpperCase() + words.slice(1);
  return unit === undefined ? name : `${name} (${unit})`;
}
