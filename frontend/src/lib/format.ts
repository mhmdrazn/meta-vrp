export function minutesToHHMM(mins: number) {
  const h = Math.floor(mins / 60)
  const m = Math.round(mins % 60)
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`
}

export function formatNodeName(raw: string) {
  const spaced = raw.replace(/_/g, ' ').trim()
  if (spaced !== spaced.toUpperCase()) return spaced
  return spaced.toLowerCase().replace(/(^|[\s(/-])([a-z])/g, (_, sep, ch) => sep + ch.toUpperCase())
}
