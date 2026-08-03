import { useQuery } from '@tanstack/react-query'
import { Api } from '../lib/api'
import type { Node } from '../types'
import { useDataset } from '../stores/dataset'

/**
 * Fetch all nodes for the currently-selected dataset.
 * Query key is `['nodes', datasetId]` so switching datasets triggers a fresh fetch
 * and each dataset caches independently.
 */
export function useAllNodes() {
  const datasetId = useDataset((s) => s.datasetId)
  return useQuery<Node[]>({
    queryKey: ['nodes', datasetId],
    queryFn: () => Api.listNodes(datasetId),
    staleTime: 60_000,
  })
}
