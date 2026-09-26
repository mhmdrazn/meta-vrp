import { useMemo, useState, useEffect, useRef } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Api } from "../lib/api";
import type { OptimizeResponse, Node, Geometry, Dataset } from "../types";
import { minutesToHHMM } from "../lib/format";
import { cn, getVehicleColor } from "../lib/utils";
import NodesMapSelector from "../components/NodesMapSelector";
import { useDataset } from "../stores/dataset";
import { useOptimizeMem } from "../stores/optimize";
import { motion, AnimatePresence } from "framer-motion";

import OptimizeResultMap from "../components/OptimizeResultMap";
import MapLegend from "../components/MapLegend";
import { useAllNodes } from "../hooks/useAllNodes";

import { Alert, AlertTitle } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Progress } from "@/components/ui/progress";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    Card,
    CardHeader,
    CardTitle,
    CardContent,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { useToast } from "@/hooks/use-toast";
import {
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from "@/components/ui/tooltip";

import {
    Loader2,
    Play,
    Trash2,
    ListChecks,
    MapPin,
    CheckCircle2,
    AlertCircle,
    ListTree,
    FileDown,
    Eye,
    EyeOff,
    Droplets,
    TreeDeciduous,
    ChevronDown,
    ChevronUp,
} from "lucide-react";

const PLANNING_MODES = [
    { value: 5, label: "5 s", title: "Rapid", description: "Fast response" },
    { value: 10, label: "10 s", title: "Standard", description: "Balanced" },
    { value: 20, label: "20 s", title: "Extended", description: "Longer search" },
] as const;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export default function OptimizePage() {
    const {
        data: nodes = [],
        isLoading: isLoadingNodes,
        isError: isErrorNodes,
    } = useAllNodes();

    const datasetsQ = useQuery<Dataset[]>({
        queryKey: ["datasets"],
        queryFn: Api.listDatasets,
        staleTime: 5 * 60_000,
    });

    const { datasetId, setDatasetId } = useDataset();

    const [timeLimitSec, setTimeLimitSec] = useState<number>(5);
    const [numVehicles, setNumVehicles] = useState<number>(7);
    const [vehicleRoutes, setVehicleRoutes] = useState<Record<number, Geometry[]>>({});
    const [isFetchingRoutes, setIsFetchingRoutes] = useState(false);
    const [isExporting, setIsExporting] = useState(false);
    const [highlightedVehicleId, setHighlightedVehicleId] = useState<number | null>(null);
    const [selectedVehicleIds, setSelectedVehicleIds] = useState<Set<number>>(new Set());
    const [showAdvanced, setShowAdvanced] = useState(true);

    const VEHICLE_COUNT_OPTIONS = useMemo(
        () => Array.from({ length: 8 }, (_, i) => i + 3), // 3..10
        [],
    );

    const mapRef = useRef<HTMLDivElement>(null);
    const summaryRef = useRef<HTMLDivElement>(null);
    const tableRef = useRef<HTMLDivElement>(null);

    const parks = useMemo(
        () => nodes.filter((node) => node.id !== "0" && node.kind === "park"),
        [nodes],
    );

    const allParkIds = useMemo(
        () => new Set(parks.map((p) => p.id)),
        [parks],
    );

    const nodesById = useMemo(
        () => new Map(nodes.map((n) => [n.id, n])),
        [nodes],
    );

    const { toast } = useToast();
    const { lastResult, lastPayload, setLastResult, clearLastResult } = useOptimizeMem();
    const [progress, setProgress] = useState(0);

    const {
        mutate,
        isPending,
        error: optimizeError,
        reset: resetOptimize,
    } = useMutation({
        mutationFn: (payload: any) => Api.optimize(payload),
        onSuccess: (res, variables) => {
            setLastResult(res, {
                num_vehicles: variables?.num_vehicles,
                selected_node_ids: variables?.selected_node_ids ?? [],
            });
            setSelectedVehicleIds(new Set((res?.routes ?? []).map((r) => r.vehicle_id)));
            toast({
                title: "Optimization Complete",
                description: `Makespan ${res.makespan?.toFixed(2) ?? res.objective_time_min} min`,
                action: <CheckCircle2 className="h-5 w-5 text-green-500" />,
            });
        },
    });

    const data: OptimizeResponse | undefined = lastResult;

    const handleDatasetChange = (newId: string) => {
        if (newId === datasetId) return;
        setDatasetId(newId);
        clearLastResult();
        setVehicleRoutes({});
        setHighlightedVehicleId(null);
        setSelectedVehicleIds(new Set());
        resetOptimize();
    };

    const handleRun = () => {
        const node_ids = parks.map((p) => p.id);
        setVehicleRoutes({});
        setHighlightedVehicleId(null);
        setSelectedVehicleIds(new Set());
        resetOptimize();
        mutate({
            num_vehicles: numVehicles,
            selected_node_ids: node_ids,
            dataset_id: datasetId,
            algorithm: "alns_hybrid",
            time_limit_sec: timeLimitSec,
        });
    };

    const handleClearResult = () => {
        clearLastResult();
        setVehicleRoutes({});
        setHighlightedVehicleId(null);
        setSelectedVehicleIds(new Set());
        resetOptimize();
    };

    const toggleVehicleVisibility = (vehicleId: number) => {
        setSelectedVehicleIds((prev) => {
            const next = new Set(prev);
            if (next.has(vehicleId)) {
                next.delete(vehicleId);
            } else {
                next.add(vehicleId);
            }
            return next;
        });
        setHighlightedVehicleId(null);
    };

    const handleSelectAllVehicles = () => {
        if (data?.routes) {
            setSelectedVehicleIds(new Set(data.routes.map((r) => r.vehicle_id)));
        }
        setHighlightedVehicleId(null);
    };

    const handleClearAllVehicles = () => {
        setSelectedVehicleIds(new Set());
        setHighlightedVehicleId(null);
    };

    useEffect(() => {
        if (data && nodesById.size > 0) {
            const fetchGeometries = async () => {
                setIsFetchingRoutes(true);
                setVehicleRoutes({});
                const newRoutes: Record<number, Geometry[]> = {};
                for (const route of data.routes) {
                    const vehId = route.vehicle_id;
                    newRoutes[vehId] = [];
                    for (let i = 0; i < route.sequence.length - 1; i++) {
                        const idA = route.sequence[i].split("#")[0];
                        const idB = route.sequence[i + 1].split("#")[0];
                        const nodeA = nodesById.get(idA);
                        const nodeB = nodesById.get(idB);
                        if (nodeA && nodeB) {
                            try {
                                const geometry = await Api.getRouteGeometry(
                                    nodeA.lon, nodeA.lat, nodeB.lon, nodeB.lat,
                                );
                                newRoutes[vehId].push(geometry);
                                setVehicleRoutes((prev) => ({
                                    ...prev,
                                    [vehId]: [...newRoutes[vehId]],
                                }));
                                await sleep(50);
                            } catch (err) {
                                console.error(`Route segment ${idA}->${idB} failed`, err);
                            }
                        }
                    }
                }
                setIsFetchingRoutes(false);
            };
            fetchGeometries();
        }
    }, [data, nodesById]);

    useEffect(() => {
        let timer: ReturnType<typeof setInterval> | undefined;
        if (isPending) {
            setProgress(0);
            const interval = 300;
            const totalDuration = (timeLimitSec + 5) * 1000;
            const increment = (interval / totalDuration) * 100;
            timer = setInterval(() => {
                setProgress((prev) => {
                    const next = prev + increment;
                    if (next >= 100) {
                        clearInterval(timer);
                        return 100;
                    }
                    return next;
                });
            }, interval);
        }
        return () => {
            clearInterval(timer);
            if (!isPending) setProgress(0);
        };
    }, [isPending, timeLimitSec]);

    const handleExportPDF = async () => {
        if (!mapRef.current || !data) return;
        const previousHighlight = highlightedVehicleId;
        setIsExporting(true);
        try {
            const { default: jsPDF } = await import("jspdf");
            const { default: html2canvas } = await import("html2canvas");
            const pdf = new jsPDF("p", "mm", "a4");
            const pageHeight = pdf.internal.pageSize.getHeight();
            const pageWidth = pdf.internal.pageSize.getWidth();
            const margin = 15;
            const contentWidth = pageWidth - margin * 2;
            let currentY = margin;

            pdf.setFont("helvetica", "bold");
            pdf.setFontSize(20);
            pdf.setTextColor(33, 33, 33);
            pdf.text("MetaVRP Optimization Report", margin, currentY);
            currentY += 10;
            pdf.setDrawColor(200, 200, 200);
            pdf.setLineWidth(0.5);
            pdf.line(margin, currentY, pageWidth - margin, currentY);
            currentY += 10;
            pdf.setFont("helvetica", "normal");
            pdf.setFontSize(11);
            pdf.setTextColor(60, 60, 60);
            const dateStr = new Date().toLocaleDateString("en-US", { dateStyle: "full" });
            pdf.text(`Report Date : ${dateStr}`, margin, currentY);
            currentY += 6;
            pdf.text(`Vehicles Used : ${data.vehicle_used}`, margin, currentY);
            currentY += 6;
            pdf.text(
                `Total Time : ${data.objective_time_min} min (${minutesToHHMM(data.objective_time_min)})`,
                margin, currentY,
            );
            currentY += 10;

            for (let i = 0; i < data.routes.length; i++) {
                const route = data.routes[i];
                setHighlightedVehicleId(route.vehicle_id);
                await sleep(800);
                const mapCanvas = await html2canvas(mapRef.current, {
                    useCORS: true, scale: 2, backgroundColor: "#ffffff",
                    ignoreElements: (el) => el.classList.contains("leaflet-control-container"),
                });
                const mapImgData = mapCanvas.toDataURL("image/png");
                const mapImgProps = pdf.getImageProperties(mapImgData);
                const mapHeight = (mapImgProps.height * contentWidth) / mapImgProps.width;

                if (currentY + mapHeight + 50 > pageHeight) {
                    pdf.addPage();
                    currentY = margin;
                }
                pdf.setFillColor(245, 245, 245);
                pdf.rect(margin, currentY, contentWidth, 10, "F");
                pdf.setFont("helvetica", "bold");
                pdf.setFontSize(12);
                pdf.setTextColor(0, 0, 0);
                pdf.text(`Vehicle #${route.vehicle_id + 1} Route`, margin + 3, currentY + 7);
                currentY += 15;
                pdf.addImage(mapImgData, "PNG", margin, currentY, contentWidth, mapHeight);
                currentY += mapHeight + 8;

                pdf.setFont("helvetica", "bold");
                pdf.setFontSize(10);
                pdf.text("Statistics & Load:", margin, currentY);
                currentY += 5;
                pdf.setFont("helvetica", "normal");
                pdf.setFontSize(10);
                pdf.setTextColor(50, 50, 50);
                pdf.text(
                    `• Total Time: ${route.total_time_min} min (${minutesToHHMM(route.total_time_min)})`,
                    margin + 5, currentY,
                );
                currentY += 5;
                const loadStr = route.load_profile_liters.join(", ");
                const fullLoadText = "• Load Profile (L): [ " + loadStr + " ]";
                const splitLoad = pdf.splitTextToSize(fullLoadText, contentWidth - 5);
                pdf.text(splitLoad, margin + 5, currentY);
                currentY += splitLoad.length * 5 + 4;

                if (currentY + 20 > pageHeight) {
                    pdf.addPage();
                    currentY = margin;
                }
                pdf.setFont("helvetica", "bold");
                pdf.setTextColor(0, 0, 0);
                pdf.text("Visit Sequence:", margin, currentY);
                currentY += 6;
                pdf.setFont("helvetica", "normal");
                pdf.setFontSize(9);
                pdf.setTextColor(40, 40, 40);

                route.sequence.forEach((id, idx) => {
                    const rawId = id.split("#")[0];
                    const node = nodesById.get(rawId);
                    const nodeName = node?.name ?? rawId;
                    let extraInfo = "";
                    if (node?.kind === "depot") extraInfo = " [DEPOT]";
                    else if (node?.kind === "refill") extraInfo = " [REFILL]";
                    else if (node?.demand) extraInfo = ` (Demand: ${node.demand.toLocaleString()} L)`;
                    const lineText = `${idx + 1}. ${nodeName}${extraInfo}`;
                    if (currentY + 5 > pageHeight - margin) {
                        pdf.addPage();
                        currentY = margin;
                    }
                    pdf.text(lineText, margin + 5, currentY);
                    currentY += 5;
                });
                currentY += 10;
            }

            const pageCount = pdf.getNumberOfPages();
            for (let i = 1; i <= pageCount; i++) {
                pdf.setPage(i);
                pdf.setFontSize(8);
                pdf.setTextColor(150);
                pdf.text(`Page ${i} of ${pageCount}`, pageWidth - margin, pageHeight - 8, { align: "right" });
            }
            pdf.save("metavrp-route-report.pdf");
        } catch (err) {
            console.error("Export error:", err);
            toast({ title: "PDF Export Failed", variant: "destructive" });
        } finally {
            setHighlightedVehicleId(previousHighlight);
            setIsExporting(false);
        }
    };

    const canRun = !isPending && parks.length > 0;

    const getRouteStats = (route: { sequence: string[] }) => {
        const routeNodes = route.sequence
            .map((id) => nodesById.get(id.split("#")[0]))
            .filter((n): n is Node => Boolean(n));
        return {
            parksServed: routeNodes.filter((n) => n.kind === "park").length,
            refillVisits: routeNodes.filter((n) => n.kind === "refill").length,
            totalStops: routeNodes.filter((n) => n.kind !== "depot").length,
        };
    };

    const filteredRoutes = data?.routes
        ? data.routes.filter((r) => selectedVehicleIds.has(r.vehicle_id))
        : [];

    // PDF export always needs the full route set (it isolates one vehicle at a time
    // via highlightedVehicleId), so the checkbox filter is bypassed while exporting.
    const mapResult = data
        ? isExporting
            ? data
            : { ...data, routes: filteredRoutes }
        : data;
    const mapVehicleRoutes = isExporting
        ? vehicleRoutes
        : Object.fromEntries(
              Object.entries(vehicleRoutes).filter(([vid]) =>
                  selectedVehicleIds.has(Number(vid)),
              ),
          );

    const totalStops = data?.routes?.reduce(
        (sum, r) => sum + getRouteStats(r).totalStops, 0,
    ) ?? 0;

    return (
        <section className="relative">
            {/* Header */}
            <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 sm:gap-4 mb-4">
                <div className="space-y-1">
                    <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">
                        Route Optimization
                    </h1>
                    <p className="text-muted-foreground text-xs sm:text-sm">
                        Plan efficient watering routes for parks using available vehicles and refill facilities.
                    </p>
                </div>
                <div className="flex items-center gap-2 px-3 sm:px-4 py-2 sm:py-2.5 border rounded-xl bg-primary/5 border-primary/20 shrink-0 self-start sm:self-auto">
                    <ListChecks className="h-4 w-4 sm:h-5 sm:w-5 text-primary" />
                    <span className="text-xs sm:text-sm text-muted-foreground">Selected</span>
                    <span className="text-xs sm:text-sm font-semibold text-primary">
                        {parks.length} parks
                    </span>
                </div>
            </div>

            {/* Main Grid: Map + Right Panel */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
                {/* Map — 7 cols on lg, 8 cols on xl */}
                <div className="lg:col-span-7 xl:col-span-8 flex flex-col gap-4 z-0" ref={mapRef}>
                    <Card className="flex flex-col">
                        <CardHeader className="flex-row items-center justify-between py-3 sm:py-4">
                            <div className="flex items-center gap-2.5 sm:gap-3">
                                <MapPin className="h-4 w-4 sm:h-5 sm:w-5 text-primary" />
                                <CardTitle className="text-base sm:text-lg">
                                    {data ? "Route Result Map" : "Park Location Map"}
                                </CardTitle>
                            </div>
                            {data && (
                                <Button variant="outline" size="sm" onClick={handleClearResult}>
                                    Close
                                </Button>
                            )}
                            {!data && (
                                <span className="text-xs sm:text-sm text-muted-foreground">
                                    {parks.length} parks in area
                                </span>
                            )}
                        </CardHeader>
                        <CardContent className="pt-0">
                            {isLoadingNodes && (
                                <Alert className="mt-4">
                                    <Loader2 className="animate-spin" />
                                    <AlertTitle>Loading</AlertTitle>
                                </Alert>
                            )}
                            {isErrorNodes && (
                                <Alert variant="destructive" className="mt-4">
                                    <AlertCircle className="h-4 w-4" />
                                    <AlertTitle>Error</AlertTitle>
                                </Alert>
                            )}
                            {nodes.length > 0 && (
                                <div
                                    className="rounded-lg border overflow-hidden h-[420px] sm:h-[520px] md:h-[620px] lg:h-[720px] xl:h-[800px]"
                                >
                                    {data && mapResult ? (
                                        <OptimizeResultMap
                                            nodes={nodes}
                                            result={mapResult}
                                            vehicleRoutes={mapVehicleRoutes}
                                            highlightedVehicleId={highlightedVehicleId}
                                            showOnlyHighlighted={isExporting}
                                            vehicleFilter={{
                                                routes: data.routes,
                                                selectedVehicleIds,
                                                onToggleVehicle: toggleVehicleVisibility,
                                                onSelectAll: handleSelectAllVehicles,
                                                onClearAll: handleClearAllVehicles,
                                            }}
                                        />
                                    ) : (
                                        <div className="relative w-full h-full">
                                            <MapLegend />
                                            <NodesMapSelector
                                                nodes={nodes}
                                                selected={allParkIds}
                                                onToggle={() => {}}
                                            />
                                        </div>
                                    )}
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    {/* Legend bar (pre-result only) */}
                    {!data && (
                        <Card className="flex-shrink-0">
                            <CardContent className="py-3">
                                <div className="flex flex-wrap items-center justify-between gap-4">
                                    <div className="flex items-center gap-4">
                                        <div className="flex items-center gap-1.5">
                                            <div className="w-3 h-3 rounded-full bg-green-500" />
                                            <span className="text-xs text-muted-foreground">&lt;10K</span>
                                        </div>
                                        <div className="flex items-center gap-1.5">
                                            <div className="w-3 h-3 rounded-full bg-yellow-500" />
                                            <span className="text-xs text-muted-foreground">10-20K</span>
                                        </div>
                                        <div className="flex items-center gap-1.5">
                                            <div className="w-3 h-3 rounded-full bg-red-500" />
                                            <span className="text-xs text-muted-foreground">&gt;20K</span>
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-4 text-xs">
                                        <div className="flex items-center gap-1.5">
                                            <TreeDeciduous className="h-3.5 w-3.5 text-primary" />
                                            <span className="text-muted-foreground">Total:</span>
                                            <span className="font-semibold">{parks.length}</span>
                                        </div>
                                        <Separator orientation="vertical" className="h-4" />
                                        <div className="flex items-center gap-1.5">
                                            <Droplets className="h-3.5 w-3.5 text-blue-500" />
                                            <span className="font-semibold">
                                                {(parks.reduce((s, p) => s + (p.demand ?? 0), 0) / 1000).toFixed(0)}K L
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            </CardContent>
                        </Card>
                    )}
                </div>

                {/* Right Panel — 5 cols on lg, 4 cols on xl */}
                <div className="lg:col-span-5 xl:col-span-4 flex flex-col gap-4 min-h-0">
                    <Card>
                        <CardHeader className="py-3 sm:py-4">
                            <CardTitle className="text-base">Planning Parameters</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            {/* Study Area */}
                            <div className="space-y-2">
                                <Label htmlFor="dataset-select">Study Area</Label>
                                <Select value={datasetId} onValueChange={handleDatasetChange}>
                                    <SelectTrigger id="dataset-select">
                                        <SelectValue placeholder="Select study area" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {(datasetsQ.data ?? []).map((d) => (
                                            <SelectItem key={d.id} value={d.id}>
                                                {d.label} – {d.park_count} parks, {d.refill_count} refills
                                            </SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>

                            {/* Fleet Size */}
                            <div className="space-y-2">
                                <Label htmlFor="fleet-size-select">Number of Trucks</Label>
                                <Select
                                    value={numVehicles.toString()}
                                    onValueChange={(v) => setNumVehicles(Number(v))}
                                >
                                    <SelectTrigger id="fleet-size-select">
                                        <SelectValue placeholder="Select fleet size" />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {VEHICLE_COUNT_OPTIONS.map((n) => (
                                            <SelectItem key={n} value={n.toString()}>
                                                {n} trucks
                                            </SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>

                            {/* Planning Mode */}
                            <div className="space-y-2">
                                <Label>
                                    Planning Mode{" "}
                                    <span className="text-muted-foreground font-normal">
                                        (Computation Budget)
                                    </span>
                                </Label>
                                <div className="grid grid-cols-3 gap-2">
                                    {PLANNING_MODES.map((mode) => (
                                        <button
                                            key={mode.value}
                                            type="button"
                                            onClick={() => setTimeLimitSec(mode.value)}
                                            className={cn(
                                                "rounded-xl border-2 p-2 sm:p-2.5 xl:p-3 text-center transition-all cursor-pointer flex flex-col items-center justify-center min-w-0",
                                                timeLimitSec === mode.value
                                                    ? "border-green-500 bg-green-500/10 text-green-700 dark:text-green-400 shadow-sm"
                                                    : "border-border hover:border-green-500/50 text-foreground",
                                            )}
                                        >
                                            <div className="text-base sm:text-lg font-bold">{mode.label}</div>
                                            <div className="text-xs sm:text-sm font-medium">{mode.title}</div>
                                            <div className="text-[10px] sm:text-[11px] text-muted-foreground leading-tight mt-0.5 line-clamp-2">
                                                {mode.description}
                                            </div>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            <Separator />

                            <Button
                                size="lg"
                                className="w-full bg-green-600 hover:bg-green-700 text-white"
                                disabled={!canRun || isPending}
                                onClick={handleRun}
                            >
                                {isPending ? (
                                    <>
                                        <Loader2 className="animate-spin mr-2" />
                                        Running...
                                    </>
                                ) : (
                                    <>
                                        <Play className="mr-2" />
                                        Run Route Planning
                                    </>
                                )}
                            </Button>

                            {isPending && (
                                <div className="space-y-2 pt-2 text-center">
                                    <Progress value={progress} className="w-full" />
                                    <p className="text-sm text-muted-foreground">
                                        Estimated: ~{timeLimitSec}s
                                    </p>
                                </div>
                            )}

                            {optimizeError && (
                                <Alert variant="destructive">
                                    <AlertTitle>Failed</AlertTitle>
                                </Alert>
                            )}
                        </CardContent>
                    </Card>

                    {/* Export / Clear */}
                    <AnimatePresence>
                        {data && (
                            <motion.div
                                initial={{ opacity: 0, y: 10 }}
                                animate={{ opacity: 1, y: 0 }}
                                className="flex flex-col sm:flex-row items-center gap-2"
                            >
                                <Button
                                    variant="outline"
                                    size="sm"
                                    className="w-full sm:flex-1 h-9 text-xs sm:text-sm font-medium"
                                    onClick={handleExportPDF}
                                    disabled={isExporting}
                                >
                                    {isExporting ? (
                                        <Loader2 className="animate-spin h-4 w-4 mr-1.5" />
                                    ) : (
                                        <FileDown className="h-4 w-4 mr-1.5" />
                                    )}
                                    Export PDF
                                </Button>
                                <Button
                                    variant="destructive"
                                    size="sm"
                                    className="w-full sm:flex-1 h-9 text-xs sm:text-sm font-medium"
                                    onClick={handleClearResult}
                                >
                                    <Trash2 className="h-4 w-4 mr-1.5" />
                                    Clear Results
                                </Button>
                            </motion.div>
                        )}
                    </AnimatePresence>

                    {isFetchingRoutes && (
                        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                            <Alert className="bg-muted/50">
                                <Loader2 className="animate-spin h-4 w-4" />
                                <AlertTitle>Loading road routes...</AlertTitle>
                            </Alert>
                        </motion.div>
                    )}

                    {/* Results Summary (KPI) — in sidebar */}
                    <AnimatePresence>
                        {data && (
                            <motion.div
                                initial={{ opacity: 0, y: 10 }}
                                animate={{ opacity: 1, y: 0 }}
                                ref={summaryRef}
                            >
                                <Card>
                                    <CardHeader className="py-3">
                                        <CardTitle className="text-base">
                                            Results Summary
                                        </CardTitle>
                                    </CardHeader>
                                    <CardContent className="space-y-3">
                                        {/* Primary KPI rows */}
                                        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-2 gap-2 sm:gap-2.5">
                                            {/* Makespan */}
                                            <div className="p-2.5 sm:p-3 rounded-lg border border-green-500/30 bg-green-500/10">
                                                <p className="text-[10px] sm:text-xs font-medium text-green-700 dark:text-green-400 mb-0.5 sm:mb-1">
                                                    Makespan
                                                </p>
                                                <p className="text-base sm:text-lg font-bold text-green-700 dark:text-green-300">
                                                    {data.makespan?.toFixed(1) ?? data.objective_time_min}
                                                    <span className="text-[10px] font-normal ml-0.5">min</span>
                                                </p>
                                            </div>
                                            {/* Fleet Time */}
                                            <div className="p-2.5 sm:p-3 rounded-lg border border-green-500/30 bg-green-500/10">
                                                <p className="text-[10px] sm:text-xs font-medium text-green-700 dark:text-green-400 mb-0.5 sm:mb-1">
                                                    Fleet Time
                                                </p>
                                                <p className="text-base sm:text-lg font-bold text-green-700 dark:text-green-300">
                                                    {data.total_time?.toFixed(1) ?? "-"}
                                                    <span className="text-[10px] font-normal ml-0.5">min</span>
                                                </p>
                                            </div>
                                            {/* Vehicles */}
                                            <div className="p-2.5 sm:p-3 rounded-lg border border-green-500/30 bg-green-500/10">
                                                <p className="text-[10px] sm:text-xs font-medium text-green-700 dark:text-green-400 mb-0.5 sm:mb-1">
                                                    Vehicles
                                                </p>
                                                <p className="text-base sm:text-lg font-bold text-green-700 dark:text-green-300">
                                                    {data.active_vehicles ?? data.vehicle_used}
                                                    <span className="text-[10px] font-normal ml-0.5">
                                                        / {lastPayload?.num_vehicles ?? numVehicles}
                                                    </span>
                                                </p>
                                            </div>
                                            {/* Feasible */}
                                            <div className="p-2.5 sm:p-3 rounded-lg border border-green-500/30 bg-green-500/10">
                                                <p className="text-[10px] sm:text-xs font-medium text-green-700 dark:text-green-400 mb-0.5 sm:mb-1">
                                                    Feasible
                                                </p>
                                                <p className={cn(
                                                    "text-base sm:text-lg font-bold",
                                                    data.feasible === false
                                                        ? "text-red-600 dark:text-red-400"
                                                        : "text-green-700 dark:text-green-300",
                                                )}>
                                                    {data.feasible === false ? "No" : "Yes"}
                                                </p>
                                            </div>
                                        </div>

                                        {/* Secondary Metrics */}
                                        <AnimatePresence>
                                            {showAdvanced && (
                                                <motion.div
                                                    initial={{ opacity: 0, height: 0 }}
                                                    animate={{ opacity: 1, height: "auto" }}
                                                    exit={{ opacity: 0, height: 0 }}
                                                    className="overflow-hidden"
                                                >
                                                    <div className="grid grid-cols-1 sm:grid-cols-3 lg:grid-cols-1 gap-2 pt-1">
                                                        <div className="p-2.5 rounded-lg bg-muted/50 space-y-0.5">
                                                            <p className="text-[10px] text-muted-foreground">
                                                                Workload Variation (Std Dev)
                                                            </p>
                                                            <p className="text-xs sm:text-sm font-semibold">
                                                                {data.route_time_std?.toFixed(2) ?? "-"} min
                                                            </p>
                                                        </div>
                                                        <div className="p-2.5 rounded-lg bg-muted/50 space-y-0.5">
                                                            <p className="text-[10px] text-muted-foreground">
                                                                Refill Visits
                                                            </p>
                                                            <p className="text-xs sm:text-sm font-semibold">
                                                                {data.refill_visits ?? "-"}
                                                            </p>
                                                        </div>
                                                        <div className="p-2.5 rounded-lg bg-muted/50 space-y-0.5">
                                                            <p className="text-[10px] text-muted-foreground">
                                                                Computation Time
                                                            </p>
                                                            <p className="text-xs sm:text-sm font-semibold">
                                                                {data.computation_time?.toFixed(2) ?? "-"} s
                                                            </p>
                                                        </div>
                                                    </div>
                                                </motion.div>
                                            )}
                                        </AnimatePresence>
                                        <button
                                            type="button"
                                            onClick={() => setShowAdvanced(!showAdvanced)}
                                            className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors ml-auto pt-1"
                                        >
                                            Advanced
                                            {showAdvanced ? (
                                                <ChevronUp className="h-3 w-3" />
                                            ) : (
                                                <ChevronDown className="h-3 w-3" />
                                            )}
                                        </button>
                                    </CardContent>
                                </Card>
                            </motion.div>
                        )}
                    </AnimatePresence>
                </div>
            </div>



            {/* ── Route Details ── */}
            <AnimatePresence>
                {data?.routes?.length ? (
                    <motion.div
                        initial={{ opacity: 0, y: 15 }}
                        animate={{ opacity: 1, y: 0 }}
                        className="mt-4"
                    >
                        <Card className="overflow-hidden" ref={tableRef}>
                            <CardHeader className="py-3 sm:py-4 border-b">
                                <div className="flex items-center justify-between flex-wrap gap-2 sm:gap-3">
                                    <CardTitle className="text-base sm:text-lg flex items-center gap-2.5 sm:gap-3">
                                        <ListTree className="h-4 w-4 sm:h-5 sm:w-5 text-primary" />
                                        Route Details
                                    </CardTitle>
                                    <span className="text-xs sm:text-sm text-muted-foreground">
                                        {totalStops} stops
                                    </span>
                                </div>
                            </CardHeader>
                            <div className="max-w-full overflow-x-auto touch-pan-x">
                                <Table className="min-w-[720px] md:min-w-[850px] lg:min-w-[900px]">
                                    <TableHeader>
                                        <TableRow className="bg-gradient-to-r from-primary/10 to-primary/5 hover:bg-gradient-to-r hover:from-primary/15 hover:to-primary/10 border-b-2 border-primary/20">
                                            <TableHead className="w-[100px] font-semibold text-primary">
                                                Vehicle
                                            </TableHead>
                                            <TableHead className="w-[140px] font-semibold text-primary">
                                                Total Time (min)
                                            </TableHead>
                                            <TableHead className="w-[100px] font-semibold text-primary">
                                                Parks Served
                                            </TableHead>
                                            <TableHead className="w-[100px] font-semibold text-primary">
                                                Refill Visits
                                            </TableHead>
                                            <TableHead className="font-semibold text-primary">
                                                Sequence (Depot → … → Depot)
                                            </TableHead>
                                            <TableHead className="w-[80px] text-right font-semibold text-primary">
                                                Action
                                            </TableHead>
                                        </TableRow>
                                    </TableHeader>
                                    <TableBody>
                                        {filteredRoutes.map((r) => {
                                            const color = getVehicleColor(r.vehicle_id);
                                            const isHighlighted =
                                                selectedVehicleIds.size === 1 &&
                                                selectedVehicleIds.has(r.vehicle_id);
                                            const stats = getRouteStats(r);

                                            return (
                                                <TableRow
                                                    key={r.vehicle_id}
                                                    className={cn(
                                                        "hover:bg-primary/5 transition-colors",
                                                        isHighlighted && "bg-primary/10",
                                                    )}
                                                >
                                                    <TableCell className="py-3">
                                                        <div className="flex items-center gap-2">
                                                            <div
                                                                className="w-3 h-3 rounded-full shadow-md ring-1 ring-white/50"
                                                                style={{ backgroundColor: color }}
                                                            />
                                                            <span className="font-medium">
                                                                Vehicle {r.vehicle_id + 1}
                                                            </span>
                                                        </div>
                                                    </TableCell>
                                                    <TableCell className="py-3">
                                                        <span className="inline-block px-2 py-1 bg-primary/10 text-primary rounded text-sm font-medium">
                                                            {r.total_time_min.toFixed(1)}
                                                        </span>
                                                    </TableCell>
                                                    <TableCell className="py-3 font-medium">
                                                        {stats.parksServed}
                                                    </TableCell>
                                                    <TableCell className="py-3 font-medium">
                                                        {stats.refillVisits}
                                                    </TableCell>
                                                    <TableCell className="text-xs py-3">
                                                        <div className="max-w-md">
                                                            {r.sequence.map((id, idx) => {
                                                                const rawId = id.split("#")[0];
                                                                const node = nodesById.get(rawId);
                                                                const name = node?.name ?? rawId;
                                                                return (
                                                                    <span key={`${id}-${idx}`}>
                                                                        {idx > 0 && " → "}
                                                                        {name}
                                                                    </span>
                                                                );
                                                            })}
                                                        </div>
                                                    </TableCell>
                                                    <TableCell className="text-right py-3">
                                                        <TooltipProvider>
                                                            <Tooltip>
                                                                <TooltipTrigger asChild>
                                                                    <Button
                                                                        variant={
                                                                            isHighlighted
                                                                                ? "default"
                                                                                : "ghost"
                                                                        }
                                                                        size="icon"
                                                                        className="h-8 w-8"
                                                                        onClick={() => {
                                                                            if (isHighlighted) {
                                                                                handleSelectAllVehicles();
                                                                            } else {
                                                                                setSelectedVehicleIds(
                                                                                    new Set([r.vehicle_id]),
                                                                                );
                                                                                setHighlightedVehicleId(r.vehicle_id);
                                                                            }
                                                                        }}
                                                                    >
                                                                        {isHighlighted ? (
                                                                            <Eye className="h-4 w-4" />
                                                                        ) : (
                                                                            <EyeOff className="h-4 w-4 text-muted-foreground" />
                                                                        )}
                                                                    </Button>
                                                                </TooltipTrigger>
                                                                <TooltipContent>
                                                                    {isHighlighted
                                                                        ? "Show all routes"
                                                                        : "Focus this route"}
                                                                </TooltipContent>
                                                            </Tooltip>
                                                        </TooltipProvider>
                                                    </TableCell>
                                                </TableRow>
                                            );
                                        })}
                                    </TableBody>
                                </Table>
                            </div>
                        </Card>
                    </motion.div>
                ) : null}
            </AnimatePresence>
        </section>
    );
}
