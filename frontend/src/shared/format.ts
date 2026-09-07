export function formatDateTime(value: unknown): string {
  if (!value) return '—';
  const text = String(value);
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
