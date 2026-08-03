// src/stores/dataset.ts — persistent choice of which dataset the demo is viewing.
import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

interface DatasetState {
  datasetId: string
  setDatasetId: (id: string) => void
}

export const useDataset = create<DatasetState>()(
  persist(
    (set) => ({
      datasetId: 'dataset_a',
      setDatasetId: (id) => set({ datasetId: id }),
    }),
    {
      name: 'meta-vrp-dataset-storage',
      storage: createJSONStorage(() => localStorage),
    },
  ),
)
