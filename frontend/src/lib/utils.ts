import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export const VEHICLE_ROUTE_COLORS = [
  '#1d4ed8',
  '#c026d3',
  '#db2777',
  '#ea580c',
  '#ca8a04',
  '#059669',
]

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function getDemandColor(demand: number = 0): string {
  if (demand < 10000) return '#16a34a' // Hijau (Green-600)
  if (demand <= 20000) return '#ca8a04' // Kuning (Yellow-600)
  return '#dc2626' // Merah (Red-600)
}

export function getVehicleColor(vehicleId: number): string {
  return VEHICLE_ROUTE_COLORS[vehicleId % VEHICLE_ROUTE_COLORS.length]
}
