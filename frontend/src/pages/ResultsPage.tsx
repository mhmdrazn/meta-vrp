import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Api } from '../lib/api'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Loader2, ListTree } from 'lucide-react'

type Tab = 'baseline' | 'scenario1' | 'scenario2'

const ALGO_LABELS: Record<string, string> = {
  ACO: 'ACO',
  ALNS: 'Standard ALNS',
  Hybrid: 'Hybrid ALNS',
}

const LEVEL_LABELS: Record<string, string> = {
  full: 'Full',
  moderate: 'Moderate',
  severe: 'Severe',
}

const DATASET_ASSET_PREFIX: Record<string, string> = {
  dataset_a: 'DatasetA',
  dataset_b: 'DatasetB',
}

function fmt(v: unknown, decimals = 2): string {
  if (v == null) return '-'
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(decimals)
  return String(v)
}

// Best value in a numeric column across the comparison rows — used to bold the row
// that stands out for that metric. direction="min" for metrics where smaller is
// better (time, fitness, std dev, visit counts, vehicles used); "max" only for
// feasible rate, where a higher percentage is better.
function columnBest(
  rows: { cells: Record<string, unknown> }[],
  key: string,
  direction: 'min' | 'max' = 'min',
): number {
  const vals = rows.map((r) => Number(r.cells[key] ?? (direction === 'min' ? Infinity : -Infinity)))
  return direction === 'min' ? Math.min(...vals) : Math.max(...vals)
}

// A metric cell that bolds itself (and picks up an accent color) when its value is
// the best in the column — a quick visual "which one wins" cue.
function MetricCell({
  value,
  best,
  decimals = 2,
  suffix = '',
}: {
  value: number
  best: number
  decimals?: number
  suffix?: string
}) {
  const isBest = Number.isFinite(value) && value === best
  return (
    <TableCell className={`text-right ${isBest ? 'font-bold text-primary' : ''}`}>
      {fmt(value, decimals)}
      {suffix}
    </TableCell>
  )
}

interface AggRow {
  key: string
  cells: Record<string, unknown>
  count: number
}

// Groups rows and reduces each metric to its BEST value across the seeds in that
// group (minimum — the seed that got closest to the objective), not the mean. The
// one exception is "feasible", which stays a rate (share of seeds that were
// feasible) since a single min/max of a boolean column would just collapse to 0 or
// 100% and throw away the reliability signal.
function aggregate(rows: Record<string, any>[], groupBy: string[], metrics: string[]): AggRow[] {
  const map = new Map<
    string,
    {
      mins: Record<string, number>
      feasibleCount: number
      count: number
      sample: Record<string, any>
    }
  >()
  for (const r of rows) {
    const key = groupBy.map((g) => String(r[g] ?? '')).join('|')
    let entry = map.get(key)
    if (!entry) {
      entry = { mins: {}, feasibleCount: 0, count: 0, sample: r }
      for (const m of metrics) entry.mins[m] = Infinity
      map.set(key, entry)
    }
    entry.count++
    for (const m of metrics) {
      if (m === 'feasible') {
        if (r[m] === true) entry.feasibleCount++
        continue
      }
      const val = r[m]
      if (typeof val === 'number' && val < entry.mins[m]) entry.mins[m] = val
    }
  }
  const result: AggRow[] = []
  for (const [key, { mins, feasibleCount, count, sample }] of map) {
    const cells: Record<string, unknown> = {}
    for (const g of groupBy) cells[g] = sample[g]
    for (const m of metrics) {
      cells[m] = m === 'feasible' ? feasibleCount / count : mins[m]
    }
    cells._count = count
    result.push({ key, cells, count })
  }
  return result
}

// Find an asset filename by substring match against the notebook's naming convention
// (e.g. "DatasetA_ACO_convergence.png"). Returns undefined if the notebook didn't
// render that asset for this combination (e.g. scenario2 has no per-level Gantt).
function findAsset(assets: string[] | undefined, ...must: string[]): string | undefined {
  if (!assets) return undefined
  return assets.find((f) => must.every((m) => f.includes(m)))
}

interface SeedColumn {
  key: string
  label: string
  decimals?: number
}

// Modal showing everything the notebook computed for one experiment cell: every
// individual seed's raw metrics, the convergence curves, the best run's Gantt chart,
// and the interactive Folium route map embedded inline. Mirrors the notebook's own
// per-(dataset, algorithm/level) output section instead of only the summarized row.
function ExperimentDetailDialog({
  title,
  description,
  seedRows,
  seedColumns,
  convergenceUrl,
  ganttUrl,
  routeMapUrl,
}: {
  title: string
  description: string
  seedRows: Record<string, any>[]
  seedColumns: SeedColumn[]
  convergenceUrl?: string
  ganttUrl?: string
  routeMapUrl?: string
}) {
  return (
    <DialogContent className='max-w-6xl w-[95vw] max-h-[90vh] overflow-y-auto'>
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>

      <div className='space-y-6'>
        <section>
          <h3 className='text-sm font-semibold mb-2'>Per-Seed Results ({seedRows.length} runs)</h3>
          <div className='overflow-x-auto border rounded-md max-h-64 overflow-y-auto'>
            <Table>
              <TableHeader className='sticky top-0 bg-card'>
                <TableRow>
                  {seedColumns.map((c) => (
                    <TableHead key={c.key} className='text-right first:text-left'>
                      {c.label}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {seedRows.map((r, i) => (
                  <TableRow key={i}>
                    {seedColumns.map((c) => (
                      <TableCell
                        key={c.key}
                        className='text-right first:text-left font-mono text-xs'
                      >
                        {fmt(r[c.key], c.decimals ?? 2)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>

        <section>
          <h3 className='text-sm font-semibold mb-2'>Convergence</h3>
          {convergenceUrl ? (
            <img
              src={convergenceUrl}
              alt='Convergence curves'
              className='w-full rounded-md border'
            />
          ) : (
            <p className='text-sm text-muted-foreground'>
              Not rendered by the notebook for this combination.
            </p>
          )}
        </section>

        <section>
          <h3 className='text-sm font-semibold mb-2'>Gantt Chart (Best Run)</h3>
          {ganttUrl ? (
            <img src={ganttUrl} alt='Gantt chart' className='w-full rounded-md border' />
          ) : (
            <p className='text-sm text-muted-foreground'>
              Not rendered by the notebook for this combination.
            </p>
          )}
        </section>

        <section>
          <h3 className='text-sm font-semibold mb-2'>Interactive Route Map</h3>
          {routeMapUrl ? (
            <iframe
              src={routeMapUrl}
              title='Interactive route map'
              className='w-full h-[520px] rounded-md border bg-white'
            />
          ) : (
            <p className='text-sm text-muted-foreground'>
              Not rendered by the notebook for this combination.
            </p>
          )}
        </section>
      </div>
    </DialogContent>
  )
}

const SEED_COLUMNS_STANDARD: SeedColumn[] = [
  { key: 'seed', label: 'Seed', decimals: 0 },
  { key: 'fitness', label: 'Fitness' },
  { key: 'total_time', label: 'Total Time' },
  { key: 'makespan', label: 'Makespan' },
  { key: 'route_time_std', label: 'Route Std' },
  { key: 'active_vehicles', label: 'Active Veh.', decimals: 0 },
  { key: 'refill_visits', label: 'Refill Visits', decimals: 0 },
  { key: 'computation_time', label: 'Compute (s)' },
  { key: 'feasible', label: 'Feasible' },
]

const SEED_COLUMNS_SCENARIO2: SeedColumn[] = [
  { key: 'subset_id', label: 'Subset', decimals: 0 },
  { key: 'seed', label: 'Seed', decimals: 0 },
  { key: 'fitness', label: 'Fitness' },
  { key: 'total_time', label: 'Total Time' },
  { key: 'makespan', label: 'Makespan' },
  { key: 'route_time_std', label: 'Route Std' },
  { key: 'refill_visits', label: 'Refill Visits', decimals: 0 },
  { key: 'distinct_refill_stations_used', label: 'Distinct Refills', decimals: 0 },
  { key: 'computation_time', label: 'Compute (s)' },
  { key: 'feasible', label: 'Feasible' },
]

function DetailTrigger() {
  return (
    <DialogTrigger asChild>
      <Button variant='ghost' size='sm' className='h-7 px-2'>
        <ListTree className='w-3.5 h-3.5 mr-1.5' /> Details
      </Button>
    </DialogTrigger>
  )
}

function BaselineTable({ dataset }: { dataset: string }) {
  const { data: rows, isLoading } = useQuery({
    queryKey: ['experiments', 'baseline', dataset],
    queryFn: () => Api.getExperiments('baseline', dataset),
    staleTime: Infinity,
  })
  const { data: assets } = useQuery({
    queryKey: ['experiments', 'baseline', 'assets', dataset],
    queryFn: () => Api.getExperimentAssets('baseline', dataset),
    staleTime: Infinity,
  })

  if (isLoading) return <Loader2 className='animate-spin mx-auto my-8' />
  if (!rows || rows.length === 0)
    return (
      <p className='text-muted-foreground text-sm py-4'>
        No baseline data available for this dataset.
      </p>
    )

  const metrics = [
    'fitness',
    'total_time',
    'makespan',
    'route_time_std',
    'active_vehicles',
    'refill_visits',
    'computation_time',
    'feasible',
  ]
  const agg = aggregate(rows, ['algorithm'], metrics)
  agg.sort((a, b) => String(a.cells.algorithm).localeCompare(String(b.cells.algorithm)))

  const dsPrefix = DATASET_ASSET_PREFIX[dataset]
  const bestFitness = columnBest(agg, 'fitness', 'min')
  const bestTotalTime = columnBest(agg, 'total_time', 'min')
  const bestMakespan = columnBest(agg, 'makespan', 'min')
  const bestRouteStd = columnBest(agg, 'route_time_std', 'min')
  const bestActiveVeh = columnBest(agg, 'active_vehicles', 'min')
  const bestRefillVisits = columnBest(agg, 'refill_visits', 'min')
  const bestComputeTime = columnBest(agg, 'computation_time', 'min')
  const bestFeasibleRate = Math.max(...agg.map((r) => Number(r.cells.feasible) * 100))

  return (
    <div className='overflow-x-auto'>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Algorithm</TableHead>
            <TableHead className='text-right'>Seeds</TableHead>
            <TableHead className='text-right'>Best Fitness</TableHead>
            <TableHead className='text-right'>Best Total Time</TableHead>
            <TableHead className='text-right'>Best Makespan</TableHead>
            <TableHead className='text-right'>Best Route Std</TableHead>
            <TableHead className='text-right'>Best Active Vehicles</TableHead>
            <TableHead className='text-right'>Best Refill Visits</TableHead>
            <TableHead className='text-right'>Best Compute (s)</TableHead>
            <TableHead className='text-right'>Feasible Rate</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {agg.map((r) => {
            const algoKey = String(r.cells.algorithm)
            const seedRows = rows
              .filter((row) => row.algorithm === algoKey)
              .sort((a, b) => a.seed - b.seed)
            const convergenceUrl = findAsset(assets, dsPrefix, algoKey, 'convergence')
            const ganttUrl = findAsset(assets, dsPrefix, algoKey, 'gantt')
            const routeMapUrl = findAsset(assets, dsPrefix, algoKey, 'route_map')

            return (
              <TableRow key={r.key}>
                <TableCell className='font-medium'>{ALGO_LABELS[algoKey] ?? algoKey}</TableCell>
                <TableCell className='text-right'>{r.count}</TableCell>
                <MetricCell value={Number(r.cells.fitness)} best={bestFitness} />
                <MetricCell value={Number(r.cells.total_time)} best={bestTotalTime} />
                <MetricCell value={Number(r.cells.makespan)} best={bestMakespan} />
                <MetricCell value={Number(r.cells.route_time_std)} best={bestRouteStd} />
                <MetricCell value={Number(r.cells.active_vehicles)} best={bestActiveVeh} />
                <MetricCell value={Number(r.cells.refill_visits)} best={bestRefillVisits} />
                <MetricCell value={Number(r.cells.computation_time)} best={bestComputeTime} />
                <MetricCell
                  value={Number(r.cells.feasible) * 100}
                  best={bestFeasibleRate}
                  decimals={0}
                  suffix='%'
                />
                <TableCell>
                  <Dialog>
                    <DetailTrigger />
                    <ExperimentDetailDialog
                      title={`${ALGO_LABELS[algoKey] ?? algoKey} (${dataset === 'dataset_a' ? 'Dataset A' : 'Dataset B'})`}
                      description="Every individual seed run, plus the notebook's convergence, Gantt chart, and route map for the best run."
                      seedRows={seedRows}
                      seedColumns={SEED_COLUMNS_STANDARD}
                      convergenceUrl={
                        convergenceUrl && Api.experimentAssetUrl('baseline', convergenceUrl)
                      }
                      ganttUrl={ganttUrl && Api.experimentAssetUrl('baseline', ganttUrl)}
                      routeMapUrl={routeMapUrl && Api.experimentAssetUrl('baseline', routeMapUrl)}
                    />
                  </Dialog>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function Scenario1Table({ dataset }: { dataset: string }) {
  const { data: rows, isLoading } = useQuery({
    queryKey: ['experiments', 'scenario1', dataset],
    queryFn: () => Api.getExperiments('scenario1', dataset),
    staleTime: Infinity,
  })
  const { data: assets } = useQuery({
    queryKey: ['experiments', 'scenario1', 'assets', dataset],
    queryFn: () => Api.getExperimentAssets('scenario1', dataset),
    staleTime: Infinity,
  })

  if (isLoading) return <Loader2 className='animate-spin mx-auto my-8' />
  if (!rows || rows.length === 0)
    return (
      <p className='text-muted-foreground text-sm py-4'>
        No scenario 1 data available for this dataset.
      </p>
    )

  const metrics = [
    'fitness',
    'total_time',
    'makespan',
    'route_time_std',
    'active_vehicles',
    'refill_visits',
    'computation_time',
    'feasible',
  ]
  const agg = aggregate(rows, ['level_label', 'n_vehicles'], metrics)

  const levelOrder = ['full', 'moderate', 'severe', 'severe2']
  agg.sort((a, b) => {
    const ai = levelOrder.indexOf(String(a.cells.level_label))
    const bi = levelOrder.indexOf(String(b.cells.level_label))
    return ai - bi
  })

  const dsPrefix = DATASET_ASSET_PREFIX[dataset]
  const bestFitness = columnBest(agg, 'fitness', 'min')
  const bestTotalTime = columnBest(agg, 'total_time', 'min')
  const bestMakespan = columnBest(agg, 'makespan', 'min')
  const bestRouteStd = columnBest(agg, 'route_time_std', 'min')
  const bestActiveVeh = columnBest(agg, 'active_vehicles', 'min')
  const bestRefillVisits = columnBest(agg, 'refill_visits', 'min')
  const bestFeasibleRate = Math.max(...agg.map((r) => Number(r.cells.feasible) * 100))

  return (
    <div className='overflow-x-auto'>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Vehicle Level</TableHead>
            <TableHead className='text-right'>Vehicles</TableHead>
            <TableHead className='text-right'>Seeds</TableHead>
            <TableHead className='text-right'>Best Fitness</TableHead>
            <TableHead className='text-right'>Best Total Time</TableHead>
            <TableHead className='text-right'>Best Makespan</TableHead>
            <TableHead className='text-right'>Best Route Std</TableHead>
            <TableHead className='text-right'>Best Active Veh.</TableHead>
            <TableHead className='text-right'>Best Refill Visits</TableHead>
            <TableHead className='text-right'>Feasible Rate</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {agg.map((r) => {
            const levelKey = String(r.cells.level_label)
            const seedRows = rows
              .filter((row) => row.level_label === levelKey)
              .sort((a, b) => a.seed - b.seed)
            const convergenceUrl = findAsset(assets, dsPrefix, 'Hybrid', levelKey, 'convergence')
            const ganttUrl = findAsset(assets, dsPrefix, 'Hybrid', levelKey, 'gantt')
            const routeMapUrl = findAsset(assets, dsPrefix, 'Hybrid', levelKey, 'route_map')

            return (
              <TableRow key={r.key}>
                <TableCell className='font-medium'>
                  <Badge variant={levelKey === 'full' ? 'default' : 'secondary'}>
                    {LEVEL_LABELS[levelKey] ?? levelKey}
                  </Badge>
                </TableCell>
                <TableCell className='text-right'>{fmt(r.cells.n_vehicles, 0)}</TableCell>
                <TableCell className='text-right'>{r.count}</TableCell>
                <MetricCell value={Number(r.cells.fitness)} best={bestFitness} />
                <MetricCell value={Number(r.cells.total_time)} best={bestTotalTime} />
                <MetricCell value={Number(r.cells.makespan)} best={bestMakespan} />
                <MetricCell value={Number(r.cells.route_time_std)} best={bestRouteStd} />
                <MetricCell value={Number(r.cells.active_vehicles)} best={bestActiveVeh} />
                <MetricCell value={Number(r.cells.refill_visits)} best={bestRefillVisits} />
                <MetricCell
                  value={Number(r.cells.feasible) * 100}
                  best={bestFeasibleRate}
                  decimals={0}
                  suffix='%'
                />
                <TableCell>
                  <Dialog>
                    <DetailTrigger />
                    <ExperimentDetailDialog
                      title={`Hybrid ALNS, ${LEVEL_LABELS[levelKey] ?? levelKey} Fleet (${dataset === 'dataset_a' ? 'Dataset A' : 'Dataset B'})`}
                      description="Every individual seed run, plus the notebook's convergence, Gantt chart, and route map for the best run."
                      seedRows={seedRows}
                      seedColumns={SEED_COLUMNS_STANDARD}
                      convergenceUrl={
                        convergenceUrl && Api.experimentAssetUrl('scenario1', convergenceUrl)
                      }
                      ganttUrl={ganttUrl && Api.experimentAssetUrl('scenario1', ganttUrl)}
                      routeMapUrl={routeMapUrl && Api.experimentAssetUrl('scenario1', routeMapUrl)}
                    />
                  </Dialog>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function Scenario2Table({ dataset }: { dataset: string }) {
  const { data: rows, isLoading } = useQuery({
    queryKey: ['experiments', 'scenario2', dataset],
    queryFn: () => Api.getExperiments('scenario2', dataset),
    staleTime: Infinity,
  })
  const { data: assets } = useQuery({
    queryKey: ['experiments', 'scenario2', 'assets', dataset],
    queryFn: () => Api.getExperimentAssets('scenario2', dataset),
    staleTime: Infinity,
  })

  if (isLoading) return <Loader2 className='animate-spin mx-auto my-8' />
  if (!rows || rows.length === 0)
    return (
      <p className='text-muted-foreground text-sm py-4'>
        No scenario 2 data available for this dataset.
      </p>
    )

  const metrics = [
    'fitness',
    'total_time',
    'makespan',
    'route_time_std',
    'refill_visits',
    'distinct_refill_stations_used',
    'computation_time',
    'feasible',
  ]
  const agg = aggregate(rows, ['refill_level'], metrics)
  agg.sort(
    (a, b) =>
      parseInt(String(b.cells.refill_level), 10) - parseInt(String(a.cells.refill_level), 10),
  )

  const dsPrefix = DATASET_ASSET_PREFIX[dataset]
  // The notebook's combined convergence grid covers every dataset x refill level in
  // one image — it isn't per-row, so it's the same asset for every level here.
  const sharedConvergenceUrl = findAsset(assets, 'scenario2_convergence')
  const bestFitness = columnBest(agg, 'fitness', 'min')
  const bestTotalTime = columnBest(agg, 'total_time', 'min')
  const bestMakespan = columnBest(agg, 'makespan', 'min')
  const bestRouteStd = columnBest(agg, 'route_time_std', 'min')
  const bestRefillVisits = columnBest(agg, 'refill_visits', 'min')
  const bestDistinctRefills = columnBest(agg, 'distinct_refill_stations_used', 'min')
  const bestFeasibleRate = Math.max(...agg.map((r) => Number(r.cells.feasible) * 100))

  return (
    <div className='overflow-x-auto'>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Refill Availability</TableHead>
            <TableHead className='text-right'>Seeds</TableHead>
            <TableHead className='text-right'>Best Fitness</TableHead>
            <TableHead className='text-right'>Best Total Time</TableHead>
            <TableHead className='text-right'>Best Makespan</TableHead>
            <TableHead className='text-right'>Best Route Std</TableHead>
            <TableHead className='text-right'>Best Refill Visits</TableHead>
            <TableHead className='text-right'>Best Distinct Refills</TableHead>
            <TableHead className='text-right'>Feasible Rate</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {agg.map((r) => {
            const levelKey = String(r.cells.refill_level)
            const seedRows = rows
              .filter((row) => row.refill_level === levelKey)
              .sort((a, b) => a.subset_id - b.subset_id || a.seed - b.seed)
            const pctToken = `${parseInt(levelKey, 10)}pct`
            const routeMapUrl = findAsset(assets, dsPrefix, pctToken, 'map')

            return (
              <TableRow key={r.key}>
                <TableCell className='font-medium'>
                  <Badge variant={levelKey === '100%' ? 'default' : 'secondary'}>{levelKey}</Badge>
                </TableCell>
                <TableCell className='text-right'>{r.count}</TableCell>
                <MetricCell value={Number(r.cells.fitness)} best={bestFitness} />
                <MetricCell value={Number(r.cells.total_time)} best={bestTotalTime} />
                <MetricCell value={Number(r.cells.makespan)} best={bestMakespan} />
                <MetricCell value={Number(r.cells.route_time_std)} best={bestRouteStd} />
                <MetricCell value={Number(r.cells.refill_visits)} best={bestRefillVisits} />
                <MetricCell
                  value={Number(r.cells.distinct_refill_stations_used)}
                  best={bestDistinctRefills}
                />
                <MetricCell
                  value={Number(r.cells.feasible) * 100}
                  best={bestFeasibleRate}
                  decimals={0}
                  suffix='%'
                />
                <TableCell>
                  <Dialog>
                    <DetailTrigger />
                    <ExperimentDetailDialog
                      title={`Refill ${levelKey} (${dataset === 'dataset_a' ? 'Dataset A' : 'Dataset B'})`}
                      description="Every individual seed run across all random refill subsets, plus the notebook's convergence grid and route map."
                      seedRows={seedRows}
                      seedColumns={SEED_COLUMNS_SCENARIO2}
                      convergenceUrl={
                        sharedConvergenceUrl &&
                        Api.experimentAssetUrl('scenario2', sharedConvergenceUrl)
                      }
                      routeMapUrl={routeMapUrl && Api.experimentAssetUrl('scenario2', routeMapUrl)}
                    />
                  </Dialog>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

const TABS: { id: Tab; label: string; description: string }[] = [
  {
    id: 'baseline',
    label: 'Baseline Comparison',
    description: '3 algorithms compared across both datasets with identical conditions.',
  },
  {
    id: 'scenario1',
    label: 'Vehicle Availability',
    description: 'Hybrid ALNS under reduced vehicle fleet (full / moderate / severe).',
  },
  {
    id: 'scenario2',
    label: 'Refill Availability',
    description: 'Hybrid ALNS under reduced refill station availability (100% / 50% / 25%).',
  },
]

export default function ResultsPage() {
  const [tab, setTab] = useState<Tab>('baseline')
  const [dataset, setDataset] = useState('dataset_a')

  const activeTab = TABS.find((t) => t.id === tab)!

  return (
    <div className='space-y-6'>
      <div>
        <h1 className='text-2xl font-bold tracking-tight'>Experiment Results</h1>
        <p className='text-muted-foreground mt-1'>
          Pre-computed experiment data from the offline experiment notebooks. Click "Details" on any
          row for per-seed statistics, convergence curves, and route maps.
        </p>
      </div>

      <div className='flex flex-col sm:flex-row gap-4'>
        <div className='space-y-1'>
          <label className='text-sm font-medium'>Experiment</label>
          <div className='flex gap-2'>
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                  tab === t.id
                    ? 'bg-primary text-primary-foreground font-medium'
                    : 'bg-muted text-muted-foreground hover:bg-muted/80'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <div className='space-y-1 sm:ml-auto'>
          <label className='text-sm font-medium'>Dataset</label>
          <Select value={dataset} onValueChange={setDataset}>
            <SelectTrigger className='w-[160px]'>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value='dataset_a'>Dataset A</SelectItem>
              <SelectItem value='dataset_b'>Dataset B</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{activeTab.label}</CardTitle>
          <p className='text-sm text-muted-foreground'>{activeTab.description}</p>
        </CardHeader>
        <CardContent>
          {tab === 'baseline' && <BaselineTable dataset={dataset} />}
          {tab === 'scenario1' && <Scenario1Table dataset={dataset} />}
          {tab === 'scenario2' && <Scenario2Table dataset={dataset} />}
        </CardContent>
      </Card>
    </div>
  )
}
