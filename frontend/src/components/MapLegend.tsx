import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ChevronDown, ChevronUp, Info } from 'lucide-react'
import { cn, getVehicleColor } from '@/lib/utils'
import { Checkbox } from '@/components/ui/checkbox'
import { Separator } from '@/components/ui/separator'

const ROUTE_COLORS = Array.from({ length: 12 }, (_, idx) => ({
  color: getVehicleColor(idx),
  label: `Vehicle ${idx + 1}`,
}))

interface VehicleFilterProps {
  routes: { vehicle_id: number }[]
  selectedVehicleIds: Set<number>
  onToggleVehicle: (vehicleId: number) => void
  onSelectAll: () => void
  onClearAll: () => void
}

interface MapLegendProps {
  vehicleFilter?: VehicleFilterProps
}

export default function MapLegend({ vehicleFilter }: MapLegendProps) {
  const [isOpen, setIsOpen] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth >= 768 : true,
  )

  return (
    // Responsive positioning: top-3 right-3 on mobile, top-4 right-4 on desktop
    <div className='absolute top-3 right-3 sm:top-4 sm:right-4 z-[1000] flex flex-col gap-2 items-end'>
      {/* Tombol Toggle */}
      <Button
        size='sm'
        variant='secondary'
        className='w-fit shadow-lg bg-white/90 backdrop-blur border border-zinc-200 dark:bg-zinc-900/90 dark:border-zinc-800 h-8 text-xs px-3 touch-manipulation'
        onClick={() => setIsOpen(!isOpen)}
      >
        <Info className='w-3 h-3 mr-2' />
        {isOpen ? 'Close' : 'Legend'}
        {isOpen ? <ChevronUp className='w-3 h-3 ml-1' /> : <ChevronDown className='w-3 h-3 ml-1' />}
      </Button>

      {/* Konten Legend */}
      {isOpen && (
        <div
          className={cn(
            'p-3 rounded-lg shadow-xl border w-48 sm:w-52 max-w-[calc(100vw-2.5rem)] text-xs',
            'bg-white/95 backdrop-blur dark:bg-zinc-950/95 border-zinc-200 dark:border-zinc-800',
            'animate-in slide-in-from-top-2 duration-200',
            // Responsive max height to stay inside map boundaries
            'max-h-[340px] sm:max-h-[440px] lg:max-h-[520px] overflow-y-auto',
          )}
        >
          <h4 className='font-semibold mb-2'>Map Legend</h4>

          <div className='space-y-1.5 mb-3'>
            <div className='font-medium text-[10px] text-muted-foreground uppercase tracking-wider'>
              Locations
            </div>
            <div className='flex items-center gap-2'>
              <div className='w-5 h-5 rounded-full border-2 border-gray-600 bg-white flex items-center justify-center shadow-sm'>
                <svg className='w-2.5 h-2.5 text-gray-600' viewBox='0 0 24 24' fill='currentColor'>
                  <path d='m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z' />
                  <polyline points='9 22 9 12 15 12 15 22' />
                </svg>
              </div>
              <span>Central Depot</span>
            </div>
            <div className='flex items-center gap-2'>
              <div className='w-5 h-5 rounded-full border-2 border-blue-600 bg-white flex items-center justify-center shadow-sm'>
                <svg className='w-2.5 h-2.5 text-blue-600' viewBox='0 0 24 24' fill='currentColor'>
                  <path d='M12 22a7 7 0 0 0 7-7c0-2-5-9-7-15-2 6-7 13-7 15a7 7 0 0 0 7 7z' />
                </svg>
              </div>
              <span>Refill Station</span>
            </div>
            <div className='flex items-center gap-2'>
              <div className='w-5 h-5 rounded-full border-2 border-green-600 bg-white flex items-center justify-center shadow-sm'>
                <svg
                  className='w-2.5 h-2.5 text-green-600'
                  viewBox='0 0 24 24'
                  fill='none'
                  stroke='currentColor'
                  strokeWidth='3'
                >
                  <path d='M8 19h8a4 4 0 0 0 3.8-5.2 6 6 0 0 0-4-11.5 6 6 0 0 0-11.5 3.6C2.8 7.9 3 12.1 8 19Z' />
                  <path d='M12 19v3' />
                </svg>
              </div>
              <span>City Park</span>
            </div>
          </div>

          <div className='space-y-1.5 mb-3'>
            <div className='font-medium text-[10px] text-muted-foreground uppercase tracking-wider'>
              Water Demand
            </div>
            <div className='flex items-center gap-2'>
              <div className='w-3 h-3 rounded bg-green-600 opacity-80'></div>
              <span>&lt; 10k Liters (Low)</span>
            </div>
            <div className='flex items-center gap-2'>
              <div className='w-3 h-3 rounded bg-yellow-600 opacity-80'></div>
              <span>10k-20k Liters (Medium)</span>
            </div>
            <div className='flex items-center gap-2'>
              <div className='w-3 h-3 rounded bg-red-600 opacity-80'></div>
              <span>&gt; 20k Liters (High)</span>
            </div>
          </div>

          {/* Vehicle Filter section — only shown when routes data is passed */}
          {vehicleFilter ? (
            <div className='space-y-1.5'>
              <Separator className='my-2' />
              <div className='font-medium text-[10px] text-muted-foreground uppercase tracking-wider'>
                Vehicle Filter
              </div>
              <div className='flex flex-col gap-1.5'>
                {vehicleFilter.routes.map((r) => {
                  const color = getVehicleColor(r.vehicle_id)
                  const checked = vehicleFilter.selectedVehicleIds.has(r.vehicle_id)
                  return (
                    <label
                      key={r.vehicle_id}
                      className='flex items-center gap-2 cursor-pointer select-none'
                    >
                      <Checkbox
                        checked={checked}
                        onCheckedChange={() => vehicleFilter.onToggleVehicle(r.vehicle_id)}
                        className='rounded-[4px] h-3.5 w-3.5'
                      />
                      <span
                        className='w-2.5 h-2.5 rounded-sm shrink-0'
                        style={{ backgroundColor: color }}
                      />
                      <span className='text-[11px]'>Vehicle {r.vehicle_id + 1}</span>
                    </label>
                  )
                })}
              </div>
              <div className='flex items-center gap-1.5 mt-1.5'>
                <Button
                  variant='outline'
                  size='sm'
                  className='flex-1 h-6 text-[10px] px-2'
                  onClick={vehicleFilter.onSelectAll}
                >
                  All
                </Button>
                <Button
                  variant='outline'
                  size='sm'
                  className='flex-1 h-6 text-[10px] px-2'
                  onClick={vehicleFilter.onClearAll}
                >
                  None
                </Button>
              </div>
            </div>
          ) : (
            /* Static vehicle route colors — shown when no filter is active (pre-result) */
            <div className='space-y-1.5'>
              <div className='font-medium text-[10px] text-muted-foreground uppercase tracking-wider'>
                Vehicle Routes
              </div>
              <div className='grid grid-cols-2 gap-2'>
                {ROUTE_COLORS.map((rc, idx) => (
                  <div key={idx} className='flex items-center gap-2'>
                    <div
                      className='w-6 h-1 rounded-full'
                      style={{ backgroundColor: rc.color }}
                    ></div>
                    <span className='text-[10px]'>Vehicle {idx + 1}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
