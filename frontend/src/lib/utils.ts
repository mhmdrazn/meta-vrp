import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export const VEHICLE_ROUTE_COLORS = [
  '#1d4ed8',
  '#c026d3',
  '#db2777',
  '#ea580c',
  '#ca8a04',
  '#059669',
  '#0ea5e9',
  '#8b5cf6',
  '#f43f5e',
  '#14b8a6',
  '#f59e0b',
  '#22c55e',
  '#ef4444',
  '#6366f1',
  '#ec4899',
  '#84cc16',
  '#10b981',
  '#f97316',
]

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function getDemandColor(demand: number = 0): string {
  if (demand < 10000) return '#16a34a' // Hijau (Green-600)
  if (demand <= 20000) return '#ca8a04' // Kuning (Yellow-600)
  return '#dc2626' // Merah (Red-600)
}

function hslToHex(h: number, s: number, l: number): string {
  const saturation = s / 100
  const lightness = l / 100
  const c = (1 - Math.abs(2 * lightness - 1)) * saturation
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = lightness - c / 2

  let r = 0
  let g = 0
  let b = 0

  if (h >= 0 && h < 60) {
    r = c
    g = x
  } else if (h >= 60 && h < 120) {
    r = x
    g = c
  } else if (h >= 120 && h < 180) {
    g = c
    b = x
  } else if (h >= 180 && h < 240) {
    g = x
    b = c
  } else if (h >= 240 && h < 300) {
    r = x
    b = c
  } else {
    r = c
    b = x
  }

  const toHex = (value: number) =>
    Math.round((value + m) * 255)
      .toString(16)
      .padStart(2, '0')

  return `#${toHex(r)}${toHex(g)}${toHex(b)}`
}

export function getVehicleColor(vehicleId: number): string {
  const normalizedVehicleId = Math.max(0, vehicleId)
  const hue = (normalizedVehicleId * 137.508) % 360
  const saturation = 72 - (normalizedVehicleId % 3) * 6
  const lightness = 52 + (normalizedVehicleId % 4) * 2

  return hslToHex(hue, saturation, lightness)
}
