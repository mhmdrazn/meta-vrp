import { create } from "zustand";
import type { OptimizeResponse } from "../types";

type OptimizeMem = {
    lastResult?: OptimizeResponse;
    lastRunAt?: string;
    lastPayload?: { num_vehicles: number; selected_node_ids: string[] };

    setLastResult: (
        r: OptimizeResponse,
        payload: OptimizeMem["lastPayload"],
    ) => void;
    clearLastResult: () => void;
};

export const useOptimizeMem = create<OptimizeMem>((set) => ({
    lastResult: undefined,
    lastRunAt: undefined,
    lastPayload: undefined,
    setLastResult: (r, payload) =>
        set({
            lastResult: r,
            lastRunAt: new Date().toISOString(),
            lastPayload: payload,
        }),
    clearLastResult: () =>
        set({
            lastResult: undefined,
            lastRunAt: undefined,
            lastPayload: undefined,
        }),
}));
