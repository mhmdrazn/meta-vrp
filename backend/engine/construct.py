from typing import Dict, List, Optional, Set, Tuple

from .data import Node, TimeMatrix


def _nearest(target_from: str, candidates: List[str], tm: TimeMatrix) -> str:
    """Helper: cari node_id terdekat dari target_from di antara list candidates."""
    if not candidates:
        raise ValueError("nearest(): candidates must be non-empty")

    best = candidates[0]
    best_t = tm.travel(target_from, best)

    for c in candidates[1:]:
        t = tm.travel(target_from, c)
        if t < best_t:
            best, best_t = c, t

    return best


def greedy_construct(
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    selected_parks: List[str],  # Ini adalah 'selected_ids_expanded' dari app.py
    depot_id: str,
    num_vehicles: int,
    vehicle_capacity: float,
    allow_refill: bool,
    refill_ids: List[str],
) -> List[List[str]]:
    """
    Versi 'Group-Aware' dari greedy construct.
    Unit kerjanya adalah 'Grup' (misal ['1#1', '1#2', '1#3']), bukan 'part'.
    """

    # --- 1. Bangun Grup dari selected_parks (parts) ---
    groups: Dict[str, List[str]] = {}
    for part_id in selected_parks:
        # Hanya proses node yang bertipe 'park'
        node = nodes.get(part_id)
        if not node or node.type != "park":
            continue

        base_id = part_id.split("#")[0]
        groups.setdefault(base_id, []).append(part_id)

    # Pastikan urutan part di dalam grup benar (1#1, 1#2, ...)
    for base_id in groups:
        groups[base_id].sort()

    # 'unserved' sekarang berisi base_id, misal: {'1', '41', '8'}
    unserved: Set[str] = set(groups.keys())

    if not unserved:
        # Jika tidak ada park yang dipilih, kembalikan rute kosong
        return [[depot_id, depot_id] for _ in range(max(1, num_vehicles))][:1]

    # --- 2. HAPUS VALIDASI 'too_big' ---
    # Validasi 'demand > capacity' sudah tidak relevan
    # karena 'expand_split_delivery' menjamin tiap part <= capacity.

    routes: List[List[str]] = []

    # --- 3. Loop Utama (Per Kendaraan) ---
    for _ in range(num_vehicles):
        route = [depot_id]
        cur = depot_id
        rem = 0.0  # Asumsi truk mulai kosong

        MAX_ITERS = 1_000_000
        iters = 0

        while unserved:
            iters += 1
            if iters > MAX_ITERS:
                raise RuntimeError(
                    "greedy_construct: iteration cap reached (possible infinite loop)"
                )

            # --- Logika 'Group-Aware' Baru ---

            # 1. Cari semua 'anchor' (part pertama) dari grup yang belum dilayani
            #    Contoh: ['1#1', '41#1', '8']
            unserved_anchors = [
                groups[base_id][0] for base_id in unserved if groups[base_id]
            ]
            if not unserved_anchors:
                break  # Tidak ada lagi yang bisa dilayani

            # 2. Pilih GRUP terdekat berdasarkan anchor-nya
            nxt_anchor = _nearest(cur, unserved_anchors, tm)
            base_id = nxt_anchor.split("#")[0]  # Misal: '1'
            parts_to_serve = groups[base_id]  # Misal: ['1#1', '1#2', '1#3']

            # 3. Coba layani SELURUH BLOK, sisipkan refill jika perlu
            block_feasible = True
            temp_block_nodes = []  # List node (refill/part) yang akan ditambahkan

            # Simpan state sementara, jangan ubah 'cur' dan 'rem' asli
            temp_cur = cur
            temp_rem = rem

            for part in parts_to_serve:
                demand = nodes[part].demand_liters

                if demand > (temp_rem + 1e-9):  # Perlu refill
                    if not allow_refill or not refill_ids:
                        block_feasible = False  # Tidak bisa refill, grup ini gagal
                        break

                    # Cek infinite loop: sudah di refill, penuh, tapi demand masih > rem
                    if temp_rem >= vehicle_capacity and temp_cur in refill_ids:
                        block_feasible = False  # Gagal, demand > kapasitas
                        break

                    # Cari refill terdekat
                    r = _nearest(temp_cur, refill_ids, tm)
                    if r != temp_cur:
                        temp_block_nodes.append(r)
                        temp_cur = r
                    temp_rem = vehicle_capacity  # Isi penuh

                    # Cek lagi: apakah setelah isi penuh, demand masih > kapasitas?
                    if demand > (temp_rem + 1e-9):
                        block_feasible = False  # Gagal, demand > kapasitas
                        break

                # --- Muatan Cukup (baik dari sisa atau setelah refill) ---
                temp_block_nodes.append(part)
                temp_rem -= demand
                temp_cur = part

            # 4. Cek hasil simulasi blok
            if block_feasible:
                # Sukses, terapkan perubahan ke rute asli
                route.extend(temp_block_nodes)
                cur = temp_cur
                rem = temp_rem
                unserved.remove(base_id)  # Tandai GRUP ini selesai
                continue  # Lanjut ke 'while unserved' untuk cari grup berikutnya
            else:
                # Grup ini tidak muat/tidak bisa dilayani oleh kendaraan ini.
                # Hentikan rute untuk kendaraan ini.
                break
            # --- Akhir Logika 'Group-Aware' ---

        route.append(depot_id)
        routes.append(route)
        if not unserved:
            break  # Semua grup sudah dilayani

    # Karena itu, kita ubah sedikit: jika masih ada unserved,
    # paksa ke rute terakhir.
    if unserved and routes:
        route = routes[-1]
        route.pop()  # Hapus depot_id terakhir
        cur = route[-1]

        # Ambil 'rem' terakhir. Asumsi kita bisa refill dulu
        if cur not in refill_ids and allow_refill and refill_ids:
            r = _nearest(cur, refill_ids, tm)
            route.append(r)
            cur = r
            rem = vehicle_capacity
        elif cur in refill_ids:
            rem = vehicle_capacity
        else:
            # Tidak bisa refill, 'rem' adalah sisa terakhir.
            # (Logic 'rem' ini rumit, kita state ulang saja)
            rem = 0.0  # Anggap 0, paksa refill di iterasi pertama
            if cur != depot_id:
                r = _nearest(cur, refill_ids, tm)
                route.append(r)
                cur = r
                rem = vehicle_capacity

        # Ulangi logika 'group-aware' untuk sisa 'unserved'
        # Ini adalah 'best effort' dan mungkin jomplang,
        # tapi tujuannya adalah validitas (semua terlayani)

        # Salin base_id yang tersisa untuk di-loop
        remaining_groups = list(unserved)

        for base_id in remaining_groups:
            if base_id not in unserved:
                continue  # Mungkin sudah terlayani oleh grup lain? (tidak mungkin)

            parts_to_serve = groups[base_id]
            block_feasible = True
            temp_block_nodes = []

            temp_cur = cur
            temp_rem = rem

            for part in parts_to_serve:
                demand = nodes[part].demand_liters
                if demand > (temp_rem + 1e-9):
                    if not allow_refill or not refill_ids:
                        block_feasible = False
                        break
                    if temp_rem >= vehicle_capacity and temp_cur in refill_ids:
                        block_feasible = False
                        break
                    r = _nearest(temp_cur, refill_ids, tm)
                    if r != temp_cur:
                        temp_block_nodes.append(r)
                        temp_cur = r
                    temp_rem = vehicle_capacity
                    if demand > (temp_rem + 1e-9):
                        block_feasible = False
                        break

                temp_block_nodes.append(part)
                temp_rem -= demand
                temp_cur = part

            if block_feasible:
                route.extend(temp_block_nodes)
                cur = temp_cur
                rem = temp_rem
                unserved.remove(base_id)
            # else: biarkan, tidak bisa dipaksa

        route.append(depot_id)  # Tutup rute terakhir
        routes[-1] = route

    return routes


def build_initial_solution(
    num_vehicles: int,
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: str = "0",
    refill_ids: Optional[List[str]] = None,
    vehicle_capacity: float = 5000.0,
    seed: Optional[int] = None,
) -> Tuple[List[List[str]], Dict[int, Dict[str, float]]]:
    """Build initial solution using notebook cell 30 logic."""
    import random
    from .evaluation import evaluate_route, rebuild_route_with_refills, rebuild_routes_from_dmap

    rng = random.Random(seed)
    park_ids = [nid for nid, n in nodes.items() if n.type == "park"]
    demands = {nid: nodes[nid].demand_liters for nid in park_ids}
    refills_available = refill_ids if refill_ids else [nid for nid, n in nodes.items() if n.type == "refill"]

    init_r = min(refills_available, key=lambda r: tm.travel(depot_id, r)) if refills_available else depot_id
    init_overhead = tm.travel(depot_id, init_r) + 5.0

    park_list = list(park_ids)
    rng.shuffle(park_list)
    total_demand = sum(demands[nd] for nd in park_ids)
    DEMAND_BUDGET = total_demand / num_vehicles * 1.1
    remaining = {nd: float(demands[nd]) for nd in park_list}

    solution: List[List[str]] = []
    delivery_map: Dict[int, Dict[str, float]] = {}

    for tid in range(num_vehicles):
        route = [depot_id]
        rdel: Dict[str, float] = {}
        load = float(vehicle_capacity)
        cur_t = init_overhead
        pos = depot_id
        delivered_vol = 0.0

        while True:
            unmet = {nd: d for nd, d in remaining.items() if d > 0.1}
            if not unmet or delivered_vol >= DEMAND_BUDGET:
                break
            cands = []
            for nd, dem in unmet.items():
                tt = tm.travel(pos, nd)
                dv = min(load, dem)
                if dv <= 0:
                    continue
                ft = cur_t + tt + 20.0 * (dv / vehicle_capacity)
                if ft + tm.travel(nd, depot_id) <= 540.0:
                    cands.append((nd, tt, dv))
            if not cands:
                if load >= vehicle_capacity - 0.1:
                    break
                if not refills_available:
                    break
                nr = min(refills_available, key=lambda r: tm.travel(pos, r))
                tr = tm.travel(pos, nr)
                if cur_t + tr + 5.0 + tm.travel(nr, depot_id) > 540.0:
                    break
                route.append(nr)
                cur_t += tr + 5.0
                load = float(vehicle_capacity)
                pos = nr
                for nd, dem in unmet.items():
                    tt = tm.travel(pos, nd)
                    dv = min(load, dem)
                    if dv <= 0:
                        continue
                    ft = cur_t + tt + 20.0 * (dv / vehicle_capacity)
                    if ft + tm.travel(nd, depot_id) <= 540.0:
                        cands.append((nd, tt, dv))
                if not cands:
                    break
            cands.sort(key=lambda x: x[1])
            nd, tt, dv = cands[0]
            dv = min(dv, DEMAND_BUDGET - delivered_vol)
            if dv <= 0:
                break
            dv = min(dv, remaining[nd])
            route.append(nd)
            rdel[nd] = rdel.get(nd, 0.0) + dv
            remaining[nd] -= dv
            delivered_vol += dv
            load -= dv
            cur_t += tt + 20.0 * (dv / vehicle_capacity)
            pos = nd
            if load < 1.0 and refills_available:
                nr = min(refills_available, key=lambda r: tm.travel(pos, r))
                tr = tm.travel(pos, nr)
                if cur_t + tr + 5.0 + tm.travel(nr, depot_id) <= 540.0:
                    route.append(nr)
                    cur_t += tr + 5.0
                    load = float(vehicle_capacity)
                    pos = nr
        route.append(depot_id)
        solution.append(route)
        delivery_map[tid] = rdel

    order = sorted(
        [
            (
                ri,
                evaluate_route(solution[ri], nodes, tm, vehicle_capacity=vehicle_capacity, delivery_amounts=delivery_map[ri])[0]
                if len(solution[ri]) > 2
                else 0.0,
            )
            for ri in range(num_vehicles)
        ],
        key=lambda x: x[1],
    )
    for ri, _ in order:
        while True:
            unmet = {nd: d for nd, d in remaining.items() if d > 0.1}
            if not unmet:
                break
            rdmap = delivery_map[ri]
            cur_t = (
                evaluate_route(solution[ri], nodes, tm, vehicle_capacity=vehicle_capacity, delivery_amounts=rdmap)[0]
                if len(solution[ri]) > 2
                else init_overhead
            )
            pos = solution[ri][-2] if len(solution[ri]) > 2 else depot_id
            cands = []
            for nd, dem in unmet.items():
                tt = tm.travel(pos, nd)
                dv = min(vehicle_capacity, dem)
                ft = cur_t + tt + 20.0 * (dv / vehicle_capacity)
                if ft + tm.travel(nd, depot_id) <= 540.0:
                    cands.append((nd, tt, dv))
            if not cands:
                break
            cands.sort(key=lambda x: x[1])
            nd, tt, dv = cands[0]
            rdmap[nd] = rdmap.get(nd, 0.0) + dv
            remaining[nd] -= dv
            seq = [n for n in park_ids if rdmap.get(n, 0.0) > 0.1]
            solution[ri] = rebuild_route_with_refills(seq, rdmap, nodes, tm, depot_id, refills_available, vehicle_capacity)

    leftover = {nd: d for nd, d in remaining.items() if d > 0.1}
    if leftover:
        for nd, d in list(leftover.items()):
            if d <= 0.1:
                continue
            best_ri, best_cost = None, float("inf")
            for ri in range(num_vehicles):
                tmp = dict(delivery_map[ri])
                tmp[nd] = tmp.get(nd, 0.0) + d
                seq = [x for x in park_ids if tmp.get(x, 0.0) > 0.1]
                route = rebuild_route_with_refills(seq, tmp, nodes, tm, depot_id, refills_available, vehicle_capacity)
                t, feas, _ = evaluate_route(route, nodes, tm, vehicle_capacity=vehicle_capacity, delivery_amounts=tmp)
                cost = t if feas else t + 1e5
                if cost < best_cost:
                    best_cost, best_ri = cost, ri
            if best_ri is None:
                best_ri = min(range(num_vehicles), key=lambda r: sum(delivery_map[r].values()))
            delivery_map[best_ri][nd] = delivery_map[best_ri].get(nd, 0.0) + d
            remaining[nd] = 0.0
            seq = [x for x in park_ids if delivery_map[best_ri].get(x, 0.0) > 0.1]
            solution[best_ri] = rebuild_route_with_refills(seq, delivery_map[best_ri], nodes, tm, depot_id, refills_available, vehicle_capacity)

    return solution, delivery_map


def repair_empty_trucks(
    solution: List[List[str]],
    delivery_map: Dict[int, Dict[str, float]],
    num_vehicles: int,
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: str = "0",
    refill_ids: Optional[List[str]] = None,
    vehicle_capacity: float = 5000.0,
) -> Tuple[List[List[str]], Dict[int, Dict[str, float]]]:
    from .evaluation import evaluate_route, rebuild_route_with_refills, rebuild_routes_from_dmap

    park_ids = set(nid for nid, n in nodes.items() if n.type == "park")

    for _ in range(num_vehicles):
        empty_trucks = [
            ri for ri in range(num_vehicles)
            if not any(nid in park_ids for nid in solution[ri][1:-1])
        ]
        if not empty_trucks:
            break
        busy_trucks = sorted(
            [ri for ri in range(num_vehicles) if ri not in empty_trucks],
            key=lambda ri: sum(delivery_map[ri].values()),
            reverse=True,
        )
        if not busy_trucks:
            break
        donor_ri = busy_trucks[0]
        target_ri = empty_trucks[0]
        donor_parks = sorted(
            [nd for nd in park_ids if delivery_map[donor_ri].get(nd, 0.0) > 0.1],
            key=lambda nd: delivery_map[donor_ri][nd],
            reverse=True,
        )
        if not donor_parks:
            break
        moved = False
        for nd in donor_parks:
            amt = delivery_map[donor_ri][nd]
            for frac in [1.0, 0.5]:
                partial = amt * frac
                if partial < 1.0:
                    continue
                tmp_donor = dict(delivery_map[donor_ri])
                tmp_donor[nd] -= partial
                if tmp_donor[nd] <= 0.1:
                    tmp_donor.pop(nd, None)
                tmp_target = dict(delivery_map[target_ri])
                tmp_target[nd] = tmp_target.get(nd, 0.0) + partial

                s_donor = rebuild_route_with_refills([x for x in park_ids if tmp_donor.get(x, 0.0) > 0.1], tmp_donor, nodes, tm, depot_id, refill_ids, vehicle_capacity)
                s_target = rebuild_route_with_refills([x for x in park_ids if tmp_target.get(x, 0.0) > 0.1], tmp_target, nodes, tm, depot_id, refill_ids, vehicle_capacity)

                t_d, f_d, _ = evaluate_route(s_donor, nodes, tm, vehicle_capacity=vehicle_capacity, delivery_amounts=tmp_donor)
                t_t, f_t, _ = evaluate_route(s_target, nodes, tm, vehicle_capacity=vehicle_capacity, delivery_amounts=tmp_target)

                if f_d and f_t:
                    delivery_map[donor_ri] = tmp_donor
                    delivery_map[target_ri] = tmp_target
                    solution[donor_ri] = s_donor
                    solution[target_ri] = s_target
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break

    solution = rebuild_routes_from_dmap(delivery_map, num_vehicles, nodes, tm, depot_id, refill_ids, vehicle_capacity)
    return solution, delivery_map
