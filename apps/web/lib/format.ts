/** Kid-readable ball-count label used across session lists and reports. */
export function formatBallCount(count: number): string {
  if (!Number.isInteger(count) || count < 0) {
    throw new Error(`ball count must be a non-negative integer, got ${count}`);
  }
  return count === 1 ? "1 ball" : `${count} balls`;
}
