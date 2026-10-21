"""Network (topology.py: echelon DAG or uniform tree) -> realistic single-product SupplyNetPy configuration -> run.

Pipeline for one dataset row:
  (roles, edges) of S/M/I/D  --configure()-->  node/edge/demand rows  --netlist()-->  SupplyNetPy
  dicts  --simulate()-->  daily network series + node checkpoints

Values are not drawn independently. A scenario archetype fixes the ranges; customer demand is sampled
first and pushed upstream; every stock level, capacity, price and freight charge then follows from the
flow a site carries (D = base units/day: share-weighted sum of its children's flows):

  forecast       D^ = D * exp(N(bias_cfg, bias_sd))                      (per site: planners' error)
  service        z = z_cfg + stage deviation + site deviation           (z_cfg = Phi^-1(config mean service))
  reorder point  s = D^(L+r) + z*sqrt((L+r)*var_d + D^^2*var_L)          (periodic-review (s,S))
  order cycle    T = EOQ cycle T_ref*sqrt(D_med/D) * noise, clipped to the scenario range;  Q = D^*T;  S = s + Q
  fixed cost     K = h*c*D*T_eoq^2/2 per order (the EOQ cost that makes T_eoq optimal; c = cost basis, below)
  freight        K per order (link cost) + freight share * value of the units received (charged at receipt)
  holding        h * c per unit-day, c = consolidated cost basis (plant unit cost reaching the site), not the
                 transfer price (network profit is consolidated: internal markups are not a cost)
  plant capacity batch = ceil(D*mfg_time/rho): one batch at a time, so throughput = D/rho
  prices         retail price / plant unit cost = G per config; log G split plant / DC tiers / retail
                 along each path (depth does not compound margins). A DC that also has customers is priced
                 as a DC tier (sell_price, to sites) and sells to customers at retail_price = its share-weighted
                 plant unit cost x G (the full path target; retail_price = sell_price at other retailers)
Planners size on the base demand rate: seasonality and trend (netlist) are not anticipated, and
neither are outages. Planning L is transit only (a DC's wait at a short supplier is not planned for; the
realised order-to-receipt time is recorded as node_results.lead_time_obs). Each config is a "stress" config
(failure-rich knob ranges) or, for a CONTROL[0] share, a neutral "control" config (see CONTROL).

Single product throughout (SupplyNetPy: one product per site). Every manufacturer makes the same
product; its recipe is one raw material per supplier parent. Suppliers are infinite sources, or (some
configs) finite: they mine at a fixed rate into a bounded store. Multi-parent sites split their orders
by stored weights (WeightedSelect); a disrupted supplier is skipped and its weight goes to the others.
"""
import copy
import functools
import heapq
import math
import random
import signal
from statistics import NormalDist

import numpy as np
import simpy
import SupplyNetPy.Components as scm

SUPPLYNETPY_COMMIT = "0.2.2 / 7cd1585"  # PyPI supplynetpy==0.2.2 (0.2.1 + setup warnings; same results, checked 2026-10-16)

# Ranges are (lo, hi) uniform, except review: a set of review periods (days), one picked per site
# (first review at U(0, r), so sites do not all review in step). lt/cycle/review are per stage: SM supplier->manufacturer,
# MI manufacturer->inventory, II inventory->inventory; M manufacturer, I DC, R site with customers.
# gross = retail price / plant unit cost (whole path, before margin_factor); plant_share = plant's part
# of log(gross). freight = share of received value PER LEG (legs compound along a path; measured transport
# ~2-8% of customer revenue in the 0.6.0 check build, p10-p90). service = range of the config's mean
# cycle-service target. unit = what one product unit is (label only). Provenance of every range: check.md
# (most are uncalibrated priors; hold_year's 0.10 floor = APQC median carrying cost).
SCENARIOS = {
    "fmcg": dict(        # grocery / fast-moving consumer goods: high volume, short legs, thin margin
        units_day=(40, 0.8), arrivals_day=(2, 6), qty_cv=(0.3, 0.8),
        lt=dict(SM=(2, 10), MI=(1, 5), II=(1, 3)), lt_cv=(0.0, 0.3),
        raw_cost=(0.2, 2.0), recipe=(1, 3), mfg_ratio=(0.3, 1.0), mfg_time=(0.5, 2),
        gross=(1.5, 2.4), plant_share=(0.25, 0.5),
        hold_year=(0.10, 0.30), cycle=dict(M=(7, 21), I=(3, 14), R=(1, 7)),
        review=dict(M=(1, 7), I=(1,), R=(1,)), service=(0.5, 0.99), freight=(0.01, 0.03), unit="consumer pack"),
    "electronics": dict(  # high value, long inbound legs, obsolescence-heavy holding cost
        units_day=(8, 0.9), arrivals_day=(0.5, 2), qty_cv=(0.3, 1.0),
        lt=dict(SM=(14, 45), MI=(5, 30), II=(2, 7)), lt_cv=(0.1, 0.5),
        raw_cost=(10, 120), recipe=(1, 3), mfg_ratio=(0.2, 0.6), mfg_time=(2, 7),
        gross=(1.3, 2.0), plant_share=(0.55, 0.85),
        hold_year=(0.10, 0.40), cycle=dict(M=(14, 45), I=(14, 30), R=(7, 14)),
        review=dict(M=(7,), I=(1, 7), R=(1,)), service=(0.5, 0.98), freight=(0.005, 0.02), unit="device"),
    "pharma": dict(      # high margin, high service target, long API lead times
        units_day=(15, 0.8), arrivals_day=(1, 3), qty_cv=(0.2, 0.6),
        lt=dict(SM=(30, 90), MI=(3, 10), II=(1, 4)), lt_cv=(0.05, 0.3),
        raw_cost=(2, 40), recipe=(1, 3), mfg_ratio=(0.5, 2.0), mfg_time=(3, 10),
        gross=(1.8, 4.5), plant_share=(0.6, 0.85),
        hold_year=(0.10, 0.30), cycle=dict(M=(30, 60), I=(14, 30), R=(7, 14)),
        review=dict(M=(7,), I=(1, 7), R=(1,)), service=(0.7, 0.995), freight=(0.003, 0.01), unit="strip / pack"),
    "industrial": dict(  # spare parts / MRO: slow, lumpy demand, expensive items
        units_day=(2, 1.0), arrivals_day=(0.1, 0.8), qty_cv=(1.0, 2.0),
        lt=dict(SM=(7, 30), MI=(3, 14), II=(2, 7)), lt_cv=(0.1, 0.4),
        raw_cost=(20, 400), recipe=(1, 3), mfg_ratio=(0.3, 1.0), mfg_time=(2, 10),
        gross=(1.6, 3.5), plant_share=(0.4, 0.7),
        hold_year=(0.10, 0.35), cycle=dict(M=(30, 60), I=(14, 60), R=(14, 30)),
        review=dict(M=(7,), I=(7,), R=(1, 7)), service=(0.5, 0.98), freight=(0.01, 0.03), unit="part"),
}
# Management regime and environment per configuration, so the data spans failure as well as good practice.
# regime: a CONTROL[0] share of configs (own random stream "{seed}/regime", so every other draw is unchanged)
# is a neutral control: unbiased forecasts (bias_mean 0), normal margins (margin_factor 1), and a mean
# service target from CONTROL[1] (APICS 90-98% cycle-service goals); stage and site z noise and bias_sd stay.
# The rest is the stress mixture below (not re-tuned for the control share).
CONTROL = (0.2, (0.90, 0.95, 0.98))
MARGIN_FACTOR = (0.03, 1.2)  # gross_eff = 1 + f*(gross - 1): near-cost price war ... normal margins
                             # (low end 0.03, v2 pilot: ~5-7% of runs loss-making, most of them electronics)
RETAIL_SHARE = (0.55, 0.85)  # retail's part of the downstream log margin; DC tiers split the rest
Z_SD = (0.3, 0.3)            # sd of the per-stage and per-site deviation of z around z_cfg
BIAS = (-0.7, 0.15)          # config mean of log(forecast / base flow): under- ... over-forecast (v2 pilot: ~15% fill < 0.7)
BIAS_SD = (0.05, 0.3)        # per-site spread of the log forecast error
CYCLE_SD = 0.25              # lognormal noise on the EOQ cycle before clipping
MAIN_SHARE = (0.5, 0.9)      # multi-parent site: main supplier's weight; the rest Dirichlet(1)
RHO = (0.6, 0.97)            # plant utilisation at base demand (config mean; per plant +- N(0, 0.05))
FINITE_SUPPLY = (0.5, (0.2, 1.0), (7, 30))  # P(config has finite suppliers), share of its suppliers, store days
SEASON = (0.3, (0.0, 0.5))   # P(no seasonality), else config amplitude of the annual sinusoid
TREND = (-0.10, 0.20)        # annual demand growth, config mean (per stream + N(0, 0.03))
TREND_PIVOT = 1095           # day where trend factor = 1 (middle of the default 365..1825 window; cfg['trend_pivot'] overrides)
OUTAGE = (0.4, (0.2, 0.7), (180, 1500), (2, 30))  # P(no outages), P(site exposed), MTBF and MTTR days (log-uniform)


def gamma_sampler(rng, mean, cv):
    """Non-negative draw with this mean and CV (constant when cv == 0)."""
    if cv <= 0:
        return lambda: mean
    k = 1 / cv**2
    return lambda: rng.gammavariate(k, mean / k)


def qty_model(seed, mean, var):
    """Customer order size 1 + X, X negative binomial (Poisson if var <= mean - 1): exactly this mean and var."""
    g, m = np.random.default_rng(seed), mean - 1
    if m <= 0:
        return lambda: 1
    if var <= m:
        return lambda: 1 + int(g.poisson(m))
    r = m * m / (var - m)
    return lambda: 1 + int(g.negative_binomial(r, r / (r + m)))


def arrival_model(env, rng, lam, amp, phase, growth, pivot=TREND_PIVOT):
    """Gaps of a Poisson process with rate lam*(1+growth)^((t-pivot)/365)*(1+amp*sin(2 pi t/365+phase)),
    by thinning against the rate bound of the next 30 days (exact: Poisson gaps are memoryless)."""
    g = math.log1p(growth) / 365
    rate = lambda t: lam * math.exp(g * (t - pivot)) * (1 + amp * math.sin(2 * math.pi * t / 365 + phase))

    def gap():
        t = t0 = env.now
        while True:
            top = lam * math.exp(g * (t - pivot + 30 * (g > 0))) * (1 + amp)
            dt = rng.expovariate(top)
            if dt > 30:
                t += 30
            elif rng.random() * top < rate(t + dt):
                return t + dt - t0
            else:
                t += dt
    return gap


class WeightedSelect(scm.SupplierSelectionPolicy):
    """Split sourcing: each order goes to one supplier drawn with the stored weights (edges' share),
    renormalised over the suppliers that can take an order (a disrupted one is skipped)."""

    def __init__(self, node, mode="dynamic", weights=None):
        super().__init__(node, mode)
        self.name, self.weights = "weighted", weights

    def select(self, order_quantity):
        self.validate_suppliers()
        links = self._active_suppliers()
        return self.node.rng.choices(links, [self.weights[x.source.ID] for x in links])[0]


def configure(roles, edges, scenario, seed, edge_years=2.0):
    """Assign every parameter. Returns config scalars + {'nodes','edges','demands'} lists of plain dict rows.
    The config knobs are drawn first, so they depend on the seed only, not on the graph.
    edge_years: years from the trend pivot to the main window's edges (only for the peak_load label)."""
    P = SCENARIOS[scenario]
    rng = random.Random(seed)
    U = lambda r: rng.uniform(*r) if len(r) == 2 else r[0]
    LU = lambda r: math.exp(rng.uniform(math.log(r[0]), math.log(r[1])))
    n = len(roles)
    par, kid = [[] for _ in range(n)], [[] for _ in range(n)]
    for u, v in edges:
        kid[u].append(v)
        par[v].append(u)
    order, indeg = [], [len(p) for p in par]
    stack = [v for v in range(n) if not indeg[v]]
    while stack:
        v = stack.pop()
        order.append(v)
        for c in kid[v]:
            indeg[c] -= 1
            if not indeg[c]:
                stack.append(c)
    stage = ["R" if r == "I" and any(roles[c] == "D" for c in kid[v]) else r for v, r in enumerate(roles)]

    # Config knobs (stored in the configs table).
    K = dict(lt_cv=U(P["lt_cv"]), margin_factor=U(MARGIN_FACTOR), gross_ratio=U(P["gross"]),
             plant_share=U(P["plant_share"]), retail_share=U(RETAIL_SHARE), service_mean=U(P["service"]),
             bias_mean=U(BIAS), bias_sd=U(BIAS_SD), rho_mean=U(RHO), hold=U(P["hold_year"]), freight=U(P["freight"]),
             finite_supply=U(FINITE_SUPPLY[1]) * (rng.random() < FINITE_SUPPLY[0]),
             season_amp=U(SEASON[1]) * (rng.random() >= SEASON[0]), season_phase=rng.uniform(0, 2 * math.pi),
             season_spread=rng.uniform(0, math.pi / 4), trend=U(TREND),  # spread <= 45 deg: network amplitude >= ~0.74x
             outage_p=U(OUTAGE[1]) * (rng.random() >= OUTAGE[0]), mtbf_mean=LU(OUTAGE[2]), mttr_mean=LU(OUTAGE[3]))
    rr = random.Random(f"{seed}/regime")
    regime = "control" if rr.random() < CONTROL[0] else "stress"
    if regime == "control":
        K.update(bias_mean=0.0, margin_factor=1.0, service_mean=rr.choice(CONTROL[1]))
    zst = {st: NormalDist().inv_cdf(K["service_mean"]) + rng.gauss(0, Z_SD[0]) for st in "MIR"}
    tref = {st: U(P["cycle"][st]) for st in "MIR"}

    # Links (I->D is a customer stream, not a link): mean lead time and its CV.
    link = {}
    for u, v in edges:
        if roles[v] != "D":
            leg = roles[u] + ("M" if roles[v] == "M" else "I")
            link[u, v] = dict(lt_mean=round(U(P["lt"][leg]), 2), lt_cv=round(K["lt_cv"] * rng.uniform(0.5, 1.5), 3))

    # Split sourcing at inventory sites: main supplier first, weights main ~ U(MAIN_SHARE), rest Dirichlet(1).
    share = {}
    for v in range(n):
        ps = par[v]
        if roles[v] == "I":
            rng.shuffle(ps)
            w = [rng.gammavariate(1, 1) for _ in ps[1:]]
            main = U(MAIN_SHARE) if w else 1.0
            share[v] = dict(zip(ps, [main] + [(1 - main) * x / sum(w) for x in w]))

    # Pass 1, children first: customer streams, then base flow of every site and supplier.
    node = [dict(node_id=v, role=roles[v], stage=stage[v], scenario=scenario) for v in range(n)]
    demands, flow = [], [0.0] * n
    # Same push-up for the peak_load label: [sum of stream sinusoids (complex), flow at pivot +- edge_years].
    acc = [np.zeros(3, complex) for _ in range(n)]
    for v in reversed(order):
        row = node[v]
        if roles[v] == "D":
            mu = min(max(rng.lognormvariate(math.log(P["units_day"][0]), P["units_day"][1]), 0.05), 2000)
            lam = min(U(P["arrivals_day"]), mu)
            q, cv = mu / lam, U(P["qty_cv"])
            qvar = 0.0 if q <= 1 else max((cv * q) ** 2, q - 1)  # Poisson floor: var >= mean of X = q - 1
            flow[v] = mu
            demands.append(dict(demand_id=v, node_id=par[v][0], units_day=round(mu, 4), arrivals_day=round(lam, 4),
                                qty_mean=round(q, 4), qty_var=round(qvar, 4), var_day=round(lam * (qvar + q * q), 4),
                                season_amp=round(K["season_amp"] * rng.uniform(0.5, 1.5), 4),  # ponytail: max 0.5*1.5 = 0.75, no cap (old 0.9 never bound)
                                season_phase=round(K["season_phase"] + rng.gauss(0, K["season_spread"]), 4),
                                trend=round(K["trend"] + rng.gauss(0, 0.03), 4)))
            m = demands[-1]
            acc[v] = mu * np.array([m["season_amp"] * np.exp(1j * m["season_phase"]),
                                    (1 + m["trend"]) ** edge_years, (1 + m["trend"]) ** -edge_years])
        elif roles[v] == "S":
            flow[v] = sum(flow[c] * node[c]["recipe"][v] for c in kid[v])
        else:
            flow[v] = sum(flow[c] * (share[c][v] if roles[c] == "I" else 1) for c in kid[v])
            acc[v] = sum(acc[c] * (share[c][v] if roles[c] == "I" else 1) for c in kid[v])
            if roles[v] == "M":
                row["recipe"] = {p: rng.randint(*P["recipe"]) for p in par[v]}
                row["mfg_time"] = round(U(P["mfg_time"]), 2)
    dvar = {m["demand_id"]: m["var_day"] for m in demands}

    # Pass 2, parents first: prices. log(gross_eff) = plant part + downstream part; the downstream part
    # is split retail_share to the last site of a path, the rest evenly over the DC tiers of the longest
    # plant-to-customer path through each site (noise x LN(0, 0.15) on each site's part). A site that has
    # customers and also supplies sites is a DC tier for its sell_price; its customers pay retail_price =
    # basis x gross_eff (basis = share-weighted plant unit cost reaching it), so no markup is applied twice.
    lg = math.log(1 + K["margin_factor"] * (K["gross_ratio"] - 1))
    up, down, basis = [0] * n, [0] * n, [0.0] * n
    for v in order:
        if roles[v] == "I":
            up[v] = 1 + max((up[p] for p in par[v] if roles[p] == "I"), default=0)
    for v in reversed(order):
        if roles[v] == "I":
            down[v] = 1 + max((down[c] for c in kid[v] if roles[c] == "I"), default=0)
    for v in order:
        row = node[v]
        noise = rng.lognormvariate(0, 0.15)
        if roles[v] == "S":
            row["raw_cost"] = round(U(P["raw_cost"]), 4)
        elif roles[v] == "M":
            raw = sum(q * node[p]["raw_cost"] for p, q in row["recipe"].items())
            row["mfg_cost"] = round(raw * U(P["mfg_ratio"]), 4)
            row["unit_cost"] = basis[v] = raw + row["mfg_cost"]
            row["sell_price"] = round(row["unit_cost"] * math.exp(K["plant_share"] * lg * noise), 4)
            row["holding_cost"] = round(K["hold"] / 365 * row["unit_cost"], 6)
        elif roles[v] == "I":
            tiers = up[v] + down[v] - 1
            part = 1 if tiers == 1 else K["retail_share"] if down[v] == 1 else (1 - K["retail_share"]) / (tiers - 1)
            basis[v] = sum(w * basis[p] for p, w in share[v].items())
            row["buy_price"] = round(sum(w * node[p]["sell_price"] for p, w in share[v].items()), 4)
            row["sell_price"] = round(row["buy_price"] * math.exp((1 - K["plant_share"]) * lg * part * noise), 4)
            row["holding_cost"] = round(K["hold"] / 365 * basis[v], 6)  # consolidated cost, not transfer price
            if stage[v] == "R":  # price to customers
                rest = max(lg - math.log(row["buy_price"] / basis[v]), 0)  # path margin left after the buy price
                row["retail_price"] = row["sell_price"] if down[v] == 1 else \
                    round(max(row["buy_price"] * math.exp(rest * noise), row["sell_price"]), 4)

    # Pass 3, children first: stock policy, capacity and order cost per site.
    med = {st: float(np.median([flow[v] for v in range(n) if stage[v] == st] or [1])) for st in "MIR"}
    g = np.random.default_rng(rng.getrandbits(32))  # assembly lead time draws
    var = [0.0] * n
    for v in reversed(order):
        row, st = node[v], stage[v]
        if roles[v] not in "MI":
            continue
        for c in kid[v]:  # customers: compound Poisson; child sites: orders of ~Q_c at rate D_c/Q_c -> D_c*Q_c
            var[v] += dvar[c] if roles[c] == "D" else (share[c][v] if roles[c] == "I" else 1) * flow[c] * node[c]["Q"]
        if roles[v] == "M":  # all raw materials must arrive: L = E[max of inbound lead times] + batch time
            lt = np.max([g.gamma(1 / e["lt_cv"] ** 2, e["lt_mean"] * e["lt_cv"] ** 2, 512) if e["lt_cv"] > 0
                         else np.full(512, e["lt_mean"]) for e in (link[p, v] for p in par[v])], axis=0)
            L, varL = float(lt.mean()) + row["mfg_time"], float(lt.var())
        else:  # one supplier per order, drawn by weight: mixture of the links' lead times
            L = sum(w * link[p, v]["lt_mean"] for p, w in share[v].items())
            varL = sum(w * link[p, v]["lt_mean"] ** 2 * (1 + link[p, v]["lt_cv"] ** 2) for p, w in share[v].items()) - L * L
        r = rng.choice(P["review"][st])
        D = flow[v]
        Dh = D * math.exp(rng.gauss(K["bias_mean"], K["bias_sd"]))
        value = basis[v]  # same basis as holding_cost, so K stays the EOQ-consistent order cost
        teoq = tref[st] * math.sqrt(med[st] / D)
        lo, hi = P["cycle"][st]
        T = max(min(max(teoq * rng.lognormvariate(0, CYCLE_SD), lo), hi), r)
        z = min(max(zst[st] + rng.gauss(0, Z_SD[1]), -1.0), 3.3)
        pipe, sd = Dh * (L + r), math.sqrt((L + r) * var[v] + Dh * Dh * max(varL, 0))
        # An upstream site must cover at least one full order of any child at its reorder point;
        # otherwise every child order is a shortage.
        floor = max([node[c]["Q"] for c in kid[v] if roles[c] == "I"], default=0)
        s = max(0, round(pipe + z * sd), floor)
        Q = max(1, round(Dh * T))
        row.update(flow_day=round(D, 4), forecast_day=round(Dh, 4), demand_var_day=round(var[v], 4),
                   lead_time=round(L, 3), lead_time_var=round(max(varL, 0), 3), review=r,
                   first_review=round(rng.uniform(0, r), 3), cycle_days=round(T, 2), eoq_days=round(teoq, 2),
                   order_cost=round(K["hold"] / 365 * value * D * teoq * teoq / 2, 4), service_z=round(z, 3),
                   service_target=round(NormalDist().cdf(z), 4), pipeline=round(pipe, 3), safety_sd=round(sd, 3),
                   s_floor=floor, s=s, S=s + Q, Q=Q, initial_level=s + Q)
        rho = min(max(K["rho_mean"] + rng.gauss(0, 0.05), 0.4), 0.99)
        if roles[v] == "M":
            row.update(rho=round(rho, 4), batch_size=math.ceil(D * row["mfg_time"] / rho))
            # Label: load at the seasonal peak and the worse window edge of the trend; capacity stays sized at
            # base (pivot-day) demand as deliberate stress, so > 1 means the plant cannot keep up there.
            a = acc[v] / D
            row["peak_load"] = round(D * row["mfg_time"] / row["batch_size"] * (1 + abs(a[0])) * max(a[1].real, a[2].real), 4)

    # Suppliers: finite ones mine at rate flow/rho into a store of `days` of flow, at least twice the plants'
    # order-up-to raw needs, and at least the largest raw order a plant can place (store_floor, set below).
    for v in order:
        rho, days, fin = min(max(K["rho_mean"] + rng.gauss(0, 0.05), 0.4), 0.99), U(FINITE_SUPPLY[2]), rng.random()
        if roles[v] == "S":
            node[v]["flow_day"] = round(flow[v], 4)
            if fin < K["finite_supply"]:
                big = 2 * sum(node[c]["S"] * node[c]["recipe"][v] for c in kid[v])
                node[v].update(rho=round(rho, 4), supply_rate=round(flow[v] / rho, 4),
                               supply_store=math.ceil(max(flow[v] * days, big)))

    # Outages (order-intake stops: SupplyNetPy semantics) at suppliers, plants and DCs without customers.
    for v in order:
        hit, mtbf, mttr = rng.random(), K["mtbf_mean"] * rng.lognormvariate(0, 0.5), K["mttr_mean"] * rng.lognormvariate(0, 0.5)
        if hit < K["outage_p"] and stage[v] in "SMI":
            node[v].update(mtbf=round(mtbf, 2), mttr=round(mttr, 2))

    # Links: cost = fixed freight per order (the buyer's K, split over a plant's raw links). The variable part,
    # freight_share x value received, is charged at receipt in _simulate (so a big catch-up order pays more).
    out_edges = []
    for (u, v), e in link.items():
        fixed = node[v]["order_cost"] / (len(par[v]) if roles[u] == "S" else 1)
        out_edges.append(dict(source=u, target=v, **e, share=round(share[v][u], 4) if v in share else 1.0,
                              cost=round(fixed * rng.uniform(0.8, 1.2), 4)))
    out_edges.sort(key=lambda e: (e["target"], par[e["target"]].index(e["source"])))  # main supplier first
    for v in range(n):
        if roles[v] == "M":
            node[v]["recipe"] = {str(p): q for p, q in node[v]["recipe"].items()}
    for v in range(n):
        if node[v].get("supply_store"):
            node[v]["supply_store"] = max(node[v]["supply_store"], store_floor(node, out_edges, v))
    knobs = {k: round(x, 4) for k, x in K.items()}
    knobs["holding_rate_year"], knobs["freight_share"] = knobs.pop("hold"), knobs.pop("freight")
    return dict(scenario=scenario, seed=seed, regime=regime, unit=P["unit"], **knobs, nodes=node, edges=out_edges, demands=demands)


def store_floor(nodes, edges, v):
    """Largest raw order supplier v can get. SupplyNetPy ships a site order whole, so an order bigger than a
    finite supplier's store never ships and the plant stalls for good. A site orders S - position, and its
    position falls to minus the units its child sites have on order with it; a child's open orders are at most
    its own S plus what its children have on order with it. So the order is at most recipe x M(plant), with
    M(u) = S_u + sum of M over u's child sites (a site with two parents counts under both)."""
    kid, M = {}, {}
    for e in edges:
        kid.setdefault(e["source"], []).append(e["target"])

    def most(u):
        if u not in M:
            M[u] = nodes[u]["S"] + sum(most(c) for c in kid.get(u, ()))
        return M[u]
    return max((nodes[p]["recipe"][str(v)] * most(p) for p in kid.get(v, ())), default=0)


def intervene(cfg, kind, node, value):
    """What-if variant: the same configured network with one change; simulate it with the base run's seed
    (netlist gives every node, link and customer stream its own random stream, so the customer orders
    are identical and the difference is attributable to the change).
      outage   value = (start day, days): a forced outage at node (supplier, plant or DC)
      capacity value = factor on a plant's batch size (throughput)
      service  value = new z at a site; s is recomputed from its stored pipeline and safety_sd
               (a finite supplier store is raised if the bigger S needs it, see store_floor)"""
    cfg = copy.deepcopy(cfg)
    row = cfg["nodes"][node]
    if kind == "outage":
        row["forced_outages"] = sorted(row.get("forced_outages", []) + [tuple(value)])
    elif kind == "capacity":
        b = max(1, round(row["batch_size"] * value))
        row["peak_load"] = round(row["peak_load"] * row["batch_size"] / b, 4)
        row["batch_size"] = b
    elif kind == "service":
        s = max(0, round(row["pipeline"] + value * row["safety_sd"]), row["s_floor"])
        row.update(service_z=value, service_target=round(NormalDist().cdf(value), 4), s=s, S=s + row["Q"],
                   initial_level=s + row["Q"])
        for r in cfg["nodes"]:  # a bigger S can raise a plant's largest raw order: keep it shippable
            if r.get("supply_store"):
                r["supply_store"] = max(r["supply_store"], store_floor(cfg["nodes"], cfg["edges"], r["node_id"]))
    else:
        raise ValueError(kind)
    cfg["variant"] = dict(kind=kind, node=node, value=value)
    return cfg


def outage_times(env, rng, row, log):
    """(time to next outage, its duration) callables for SupplyNetPy: forced outages merged with a renewal
    process (exponential gaps, mean mtbf; lognormal durations, mean mttr, CV 1). An outage that would start
    inside the previous one is skipped. Each outage is appended to log as (start, days)."""
    def renewal():
        t, sig = 0.0, math.sqrt(math.log(2))
        while True:
            t += rng.expovariate(1 / row["mtbf"])
            d = rng.lognormvariate(math.log(row["mttr"]) - sig * sig / 2, sig)
            yield t, d
            t += d
    seq = heapq.merge(row.get("forced_outages", []), renewal()) if row.get("mtbf") else iter(row["forced_outages"])

    def until_next():
        if env.active_process is None:  # SupplyNetPy calls it once at construction to check it: not an outage
            return 1.0
        for t, d in seq:
            if t >= env.now:
                log.append((t, d))
                return max(t - env.now, 1e-9)
        return 1e12  # forced outages only, all done

    return until_next, lambda: log[-1][1] if env.active_process else 1.0


def netlist(cfg, rng):
    """Plain rows -> SupplyNetPy node/link/demand dicts + their simpy env. Every node, link and customer
    stream gets its own random stream, derived from rng in a fixed order. env.outages[node ID] logs each
    outage as (start, days)."""
    env = simpy.Environment()
    env.outages = {}
    sub = {(k, r[i]): random.Random(rng.getrandbits(64)) for k, rows, i in (
        ("n", cfg["nodes"], "node_id"), ("o", cfg["nodes"], "node_id"), ("d", cfg["demands"], "demand_id"),
        ("q", cfg["demands"], "demand_id"))
        for r in rows}
    lsub = [random.Random(rng.getrandbits(64)) for _ in cfg["edges"]]
    raw = {}
    for r in cfg["nodes"]:
        if r["role"] == "S":
            rate = r.get("supply_rate")
            q = max(1, round(rate)) if rate else 1
            raw[r["node_id"]] = scm.RawMaterial(ID=f"rm{r['node_id']}", name=f"rm{r['node_id']}", extraction_quantity=q,
                                                extraction_time=q / rate if rate else 1, mining_cost=0, cost=r["raw_cost"])
    weights = {}
    for e in cfg["edges"]:
        weights.setdefault(e["target"], {})[f"n{e['source']}"] = e["share"]
    nodes = []
    for row in cfg["nodes"]:
        v, role = row["node_id"], row["role"]
        d = dict(ID=f"n{v}", name=f"n{v}", rng=sub["n", v])
        if row.get("mtbf") or row.get("forced_outages"):
            nxt, dur = outage_times(env, sub["o", v], row, env.outages.setdefault(f"n{v}", []))
            d.update(node_disrupt_time=nxt, node_recovery_time=dur)
        if role == "S":
            if row.get("supply_rate"):
                d.update(node_type="supplier", capacity=row["supply_store"], initial_level=row["supply_store"])
            else:
                d.update(node_type="infinite_supplier")
            nodes.append(dict(d, raw_material=raw[v]))
        elif role in "MI":
            policy = dict(s=row["s"], S=row["S"], period=row["review"], first_review_delay=row["first_review"])
            d.update(node_type="manufacturer" if role == "M" else "warehouse", capacity=float("inf"),
                     initial_level=row["initial_level"], inventory_holding_cost=row["holding_cost"],
                     replenishment_policy=scm.SSReplenishment, policy_param=policy,
                     product_sell_price=row["sell_price"], supplier_selection_mode="dynamic")
            if role == "M":
                d["product"] = scm.Product(
                    ID="P", name="P", manufacturing_cost=row["mfg_cost"], manufacturing_time=row["mfg_time"],
                    sell_price=row["sell_price"], buy_price=0, batch_size=row["batch_size"],
                    raw_materials=[(raw[int(p)], q) for p, q in row["recipe"].items()])
            else:
                d.update(product_buy_price=row["buy_price"],
                         supplier_selection_policy=functools.partial(WeightedSelect, weights=weights[v]))
            nodes.append(d)
    links = [dict(ID=f"l{e['source']}_{e['target']}", source=f"n{e['source']}", sink=f"n{e['target']}",
                  cost=e["cost"], lead_time=gamma_sampler(lr, e["lt_mean"], e["lt_cv"]))
             for e, lr in zip(cfg["edges"], lsub)]
    demands = [dict(ID=f"d{m['demand_id']}", name=f"d{m['demand_id']}", demand_node=f"n{m['node_id']}",
                    order_arrival_model=arrival_model(env, sub["d", m["demand_id"]], m["arrivals_day"],
                                                      m["season_amp"], m["season_phase"], m["trend"],
                                                      cfg.get("trend_pivot", TREND_PIVOT)),
                    order_quantity_model=qty_model(sub["q", m["demand_id"]].getrandbits(64), m["qty_mean"], m["qty_var"]),
                    consume_available=True)
               for m in cfg["demands"]]
    return nodes, links, demands, env


# profit = external: customer sales at the selling site's price - (plants' raw purchases + mfg + carry + transport),
# internal transfers excluded (= dataset profit_day); book_profit = sum of sites' revenue - node_cost.
SERIES = ["ext_revenue", "net_cost", "profit", "cust_demand", "cust_served", "inventory", "site_backorder", "book_profit"]


def simulate(cfg, seed, horizon, checkpoints=(), timeout=0):
    """Run one replication; returns (daily network series [horizon, len(SERIES)], cumulative except
    inventory and site_backorder (profit = external, book_profit = booked over sites), which are levels at the end of the day; {t: per-node stat rows}
    at each checkpoint day). inventory = product units on hand at plants, DCs and retailers only: finite
    suppliers hold raw material (other units), so their level is in the node rows but not in this sum.
    site_backorder = units of site-to-site orders still waiting at their supplier (SupplyNetPy 0.2.x stats.backorder; customers here never wait, consume_available).
    Node rows (sites, customer streams, then infinite suppliers) are cumulative counters (zero_days: days a
    site ended with nothing on hand; outage_days: days it was disrupted, from netlist's env.outages log;
    level_days = exact time-integral of its level; shortage at a site = its _track site_short, customer
    units lost are added by the caller); level / level_days are None for customers and infinite suppliers.
    transport = fixed freight per order (link cost) + freight_share x purchase (value received), and cost
    includes the latter. lead (plants and DCs only, else None) = the site's list of (receipt day, order-to-receipt
    days), one per order received so far (DC: product orders, waiting at the supplier included; plant: raw orders);
    the same list object in every snapshot.
    timeout > 0: raise TimeoutError after that many wall seconds, network construction included
    (SIGALRM, so main thread only; re-fires every second in case SupplyNetPy swallows it)."""
    if not timeout:
        return _simulate(cfg, seed, horizon, checkpoints)

    def alarm(*_):
        raise TimeoutError(f"run exceeded {timeout}s")
    old = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout, 1)
    try:
        return _simulate(cfg, seed, horizon, checkpoints)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _simulate(cfg, seed, horizon, checkpoints):
    rng = random.Random(seed)
    net = scm.create_sc_net(*netlist(cfg, rng))
    env = net["env"]
    down = lambda x, t: sum(max(0.0, min(t, a + d) - a) for a, d in env.outages.get(x.ID, ()) if a < t)
    sites = [x for x in net["nodes"].values() if x.node_type != "infinite_supplier"]
    infs = [x for x in net["nodes"].values() if x.node_type == "infinite_supplier"]
    custs = list(net["demands"].values())
    lead = {}
    for x in sites:
        _track(x.inventory)
        if x.node_type in ("manufacturer", "warehouse"):
            _time_orders(x, lead.setdefault(x.ID, []))
    fr = cfg["freight_share"]  # variable freight on value received (stats.inventory_spend_cost)
    out = np.zeros((horizon, len(SERIES)))
    snaps, cps = {}, set(checkpoints)
    ns = len(sites)
    zero = [0] * ns
    price = {f"d{m['demand_id']}": cfg["nodes"][m["node_id"]]["retail_price"] for m in cfg["demands"]}
    plant = [x.node_type == "manufacturer" for x in sites]
    prod = [x.node_type != "supplier" for x in sites]  # product-unit stock; finite suppliers hold raw material
    scm.global_logger.disable_logging()
    for t in range(1, horizon + 1):
        env.run(until=t)
        rev = cost = inv = bo = net = 0.0
        for k, x in enumerate(sites):
            st = x.stats
            st.update_stats()
            vf = fr * st.inventory_spend_cost * (x.ID in lead)
            rev += st.revenue
            cost += st.node_cost + vf
            net += (st.inventory_carry_cost + st.transportation_cost + vf + getattr(st, "total_manufacturing_cost", 0)
                    + plant[k] * st.inventory_spend_cost)
            lv = x.inventory.level
            inv += prod[k] * lv
            zero[k] += lv <= 0
            bo += st.backorder[1]
        ext = sum(c.stats.fulfillment_received[1] * price[c.ID] for c in custs)
        out[t - 1] = (ext, net, ext - net, sum(c.stats.demand_placed[1] for c in custs),
                      sum(c.stats.fulfillment_received[1] for c in custs), inv, bo, rev - cost)
        if t in cps:
            for x in infs:
                x.stats.update_stats()
            vf = lambda x: fr * x.stats.inventory_spend_cost * (x.ID in lead)
            snaps[t] = [dict(node=x.ID, level=x.inventory.level if k < ns else None, revenue=x.stats.revenue,
                             cost=x.stats.node_cost + vf(x), purchase=x.stats.inventory_spend_cost,
                             mfg=getattr(x.stats, "total_manufacturing_cost", 0), carry=x.stats.inventory_carry_cost,
                             transport=x.stats.transportation_cost + vf(x), lead=lead.get(x.ID),
                             received=x.stats.demand_received[1], fulfilled=x.stats.demand_fulfilled[1],
                             placed=x.stats.demand_placed[1], got=x.stats.fulfillment_received[1],
                             shortage=x.inventory.site_short if k < ns else x.stats.shortage[1],
                             site_backorder=x.stats.backorder[1] if k < ns else 0,
                             level_days=x.inventory.level_days if k < ns else None,
                             produced=getattr(x.stats, "total_products_manufactured", 0),
                             zero_days=zero[k] if k < ns else 0, outage_days=down(x, t))
                        for k, x in enumerate(sites + custs + infs)]
    return out, snaps


def _time_orders(x, log):
    """Append (receipt day, order-to-receipt days) to log for each order site x places and receives (instance
    patch; SupplyNetPy starts them as env.process(self.process_order...)). An order that is not placed (supplier
    disrupted) returns at once, so it never reaches env.now > t0 (link lead times are > 0)."""
    def timed(f):
        def run(*args):
            t0 = x.env.now
            yield from f(*args)
            if x.env.now > t0:
                log.append((x.env.now, x.env.now - t0))
        return run
    if x.node_type == "manufacturer":
        x.process_order_raw = timed(x.process_order_raw)
    else:
        x.process_order = timed(x.process_order)


def _track(inv):
    """Two counters on a site's Inventory (instance patches; SupplyNetPy calls both methods on self):
    level_days = exact time-integral of the level (update_carry_cost runs before every level change and in
    update_stats; independent of the holding cost, so finite suppliers have it too);
    site_short = units of site orders that could not ship when placed (SupplyNetPy ships a site order whole or
    queues it: the same test as its backorder count). SupplyNetPy's own shortage (order - level) credits the
    same stock to every queued order (10 on hand, two waiting orders of 20 -> 20 short, though nothing ships)."""
    carry, get = inv.update_carry_cost, inv.get
    inv.level_days = inv.site_short = 0.0

    def update_carry_cost():
        inv.level_days += inv.inventory.level * (inv.env.now - inv.last_update_t)
        carry()

    def take(amount, site_order=False):
        ev = get(amount, site_order=site_order)
        if site_order and not ev[0].triggered:
            inv.site_short += amount
        return ev
    inv.update_carry_cost, inv.get = update_carry_cost, take


def mser5(y):
    """MSER-5 truncation point (White 1997): batch into means of 5, pick the cut d that
    minimises var(rest)/len(rest)^2 over the first half. Returns the cut in raw steps."""
    b = np.asarray(y[: len(y) // 5 * 5], float).reshape(-1, 5).mean(1)
    k = len(b)
    best, cut = math.inf, 0
    for d in range(k // 2):
        rest = b[d:]
        stat = rest.var() / len(rest)
        if stat < best:
            best, cut = stat, d
    return cut * 5


if __name__ == "__main__":
    from topology import echelon
    from tree_grammar import Grammar, Sampler, decode
    # 1. A DAG (diamonds, tier skips) and a tree configure and run in every scenario.
    roles, edges, _ = echelon(40, 3)
    multi = [v for v in range(len(roles)) if sum(e[1] == v for e in edges) > 1 and roles[v] == "I"]
    assert multi, "test graph should have multi-sourced sites"
    for sc in SCENARIOS:
        for rl, ed in ((roles, edges), decode(Sampler(Grammar((2, 1, 3, 2)), 0).at(0))):
            cfg = configure(rl, ed, sc, seed=1)
            w = {}
            for e in cfg["edges"]:
                w[e["target"]] = w.get(e["target"], 0) + e["share"]
            assert all(abs(x - 1) < 1e-3 for t, x in w.items() if rl[t] == "I"), "weights sum to 1"
            assert all(r["s"] < r["S"] and r["sell_price"] > r.get("buy_price", r.get("unit_cost", 0))
                       for r in cfg["nodes"] if "s" in r)
            y, snaps = simulate(cfg, seed=2, horizon=300, checkpoints=(300,))
            assert y[-1, 3] > 0 and 0 < y[-1, 4] <= y[-1, 3]
        print(sc, "fill %.3f profit/day %.1f" % (y[-1, 4] / y[-1, 3], y[-1, 2] / 300))
    # 1b. Network inventory = product units at plants and sites; finite suppliers' raw stock only in node rows.
    cfg = configure(roles, edges, "fmcg", seed=2)  # seed 2: two finite suppliers
    fin = {f"n{r['node_id']}" for r in cfg["nodes"] if r["role"] == "S" and r.get("supply_rate")}
    y, snaps = simulate(cfg, seed=2, horizon=100, checkpoints=(100,))
    lv = {r["node"]: r["level"] for r in snaps[100] if r["level"] is not None}
    assert fin and sum(lv[x] for x in fin) > 0, "test needs stocked finite suppliers"
    assert abs(y[-1, 5] - sum(v for x, v in lv.items() if x not in fin)) < 1e-6, "finite suppliers excluded"
    # 1c. A finite store holds the largest raw order: recipe x (S of the plant and of every site below it).
    toy = [dict(), dict(recipe={"0": 3}, S=10), dict(S=5), dict(S=2), dict(S=1)]
    assert store_floor(toy, [dict(source=0, target=1), dict(source=1, target=2), dict(source=2, target=3),
                             dict(source=1, target=4)], 0) == 3 * (10 + 5 + 2 + 1)
    for c in [cfg] + [intervene(cfg, "service", r["node_id"], 3.3) for r in cfg["nodes"] if r["role"] in "MI"]:
        assert all(r["supply_store"] >= store_floor(c["nodes"], c["edges"], r["node_id"])
                   for r in c["nodes"] if r.get("supply_store")), "finite store too small for a raw order"
    # 2. Customer streams: realised order rate and size moments = declared (thinning; 1 + negative binomial).
    class Clock:
        now = 0.0
    c, k = Clock(), 0
    gap = arrival_model(c, random.Random(1), 3.0, 0.4, 1.0, 0.0)
    peak = 0
    while c.now < 3650:
        c.now += gap()
        k += 1
        peak += math.sin(2 * math.pi * c.now / 365 + 1.0) > 0
    assert abs(k / 3650 / 3 - 1) < 0.03 and peak / k > 0.6, (k, peak)
    for mean, v in ((4.0, 9.0), (4.0, 1.0), (1.0, 0.0)):
        q = qty_model(5, mean, v)
        x = np.array([q() for _ in range(100_000)])
        assert abs(x.mean() / mean - 1) < 0.02 and abs(x.var() - max(v, mean - 1)) < 0.05 * max(v, 1), (mean, v, x.var())
    # 3. What-if variants on a small chain: same customers (own random streams), one change each.
    roles, edges = ("S", "M", "I", "I", "D", "D"), ((0, 1), (1, 2), (1, 3), (2, 4), (3, 5))
    base = configure(roles, edges, "fmcg", seed=4)
    for r in base["nodes"]:
        for x in ("mtbf", "mttr", "supply_rate"):
            r.pop(x, None)
    fill = lambda y, a, b: (y[b - 1, 4] - y[a - 1, 4]) / (y[b - 1, 3] - y[a - 1, 3])
    yb, sb = simulate(base, seed=9, horizon=900, checkpoints=(300, 400, 900))
    ycap, _ = simulate(intervene(base, "capacity", 1, 0.5), seed=9, horizon=900)
    yout, so = simulate(intervene(base, "outage", 0, (300, 100)), seed=9, horizon=900, checkpoints=(300, 400, 900))
    assert (yb[:, 3] == ycap[:, 3]).all() and (yb[:, 3] == yout[:, 3]).all(), "same customer orders"
    assert fill(ycap, 300, 900) < fill(yb, 300, 900) - 0.1, "halved plant capacity binds"
    assert fill(yout, 330, 450) < fill(yb, 330, 450) - 0.2, "supplier outage reaches customers"
    assert fill(yout, 600, 900) > fill(yb, 600, 900) - 0.05, "and the network recovers"
    raw = lambda s, t: next(r["placed"] for r in s[t] if r["node"] == "n1")  # plant's raw-material orders
    assert raw(so, 400) == raw(so, 300) and raw(so, 900) > raw(so, 400), "no orders during outage, resumed after"
    assert next(r["outage_days"] for r in so[900] if r["node"] == "n0") == 100, "infinite supplier outage logged"
    for r in sb[900]:  # level integral = carry / holding cost; site shortage <= units ordered from it
        h = next((x.get("holding_cost") for x in base["nodes"] if f"n{x['node_id']}" == r["node"]), None)
        if h:
            assert abs(r["level_days"] * h - r["carry"]) <= 1e-6 * max(1, r["carry"]) and 0 <= r["shortage"] <= r["received"]
    print("fill base %.3f, plant capacity x0.5 %.3f, supplier outage days 300-400: %.3f (days 330-450 %.3f vs %.3f)"
          % (fill(yb, 300, 900), fill(ycap, 300, 900), fill(yout, 300, 900), fill(yout, 330, 450), fill(yb, 330, 450)))
    # 4. Split sourcing skips a disrupted supplier: no orders reach it during its outage.
    cfg = configure(*echelon(40, 3)[:2], "fmcg", seed=1)
    p = next(e["source"] for e in cfg["edges"] if e["target"] in multi and  # a parent without own customers
             not any(m["node_id"] == e["source"] for m in cfg["demands"]))
    y, s = simulate(intervene(cfg, "outage", p, (100, 100)), seed=2, horizon=200, checkpoints=(110, 200))
    got = lambda t: next(r["received"] for r in s[t] if r["node"] == f"n{p}")
    assert got(200) == got(110), "orders sent to a disrupted supplier"
    # 5. Prices: customers pay ~ plant unit cost x gross_eff on every path, also at DCs that have customers.
    roles, edges = ("S", "M", "I", "I", "I", "D", "D", "D"), ((0, 1), (1, 2), (2, 3), (3, 4), (2, 5), (3, 6), (4, 7))
    for sc in SCENARIOS:
        for sd in range(5):
            cfg = configure(roles, edges, sc, seed=sd)
            lg = math.log(1 + cfg["margin_factor"] * (cfg["gross_ratio"] - 1))
            for r in cfg["nodes"][2:5]:
                g = math.log(r["retail_price"] / cfg["nodes"][1]["unit_cost"]) / lg
                assert r["retail_price"] >= r["sell_price"] and 0.6 < g < 1.4, (sc, sd, r["node_id"], g)
    # 6. Regime: ~CONTROL[0] of configs are controls, from their own stream: forcing the regime changes only
    # bias_mean / margin_factor / service_mean (and what follows from them), not the other draws.
    regs = [configure(roles, edges, "fmcg", seed=sd)["regime"] for sd in range(400)]
    assert abs(regs.count("control") / 400 - CONTROL[0]) < 0.06, regs.count("control")
    keep, out = CONTROL, {}
    for p in (0.0, 1.0):
        CONTROL = (p, keep[1])
        out[p] = configure(roles, edges, "pharma", seed=3)
    CONTROL = keep
    a, b = out[0.0], out[1.0]
    assert a["regime"] == "stress" and b["regime"] == "control" and b["bias_mean"] == 0 and b["margin_factor"] == 1
    assert b["service_mean"] in CONTROL[1] and a["gross_ratio"] == b["gross_ratio"] and a["season_spread"] == b["season_spread"]
    assert [e["lt_mean"] for e in a["edges"]] == [e["lt_mean"] for e in b["edges"]] and a["demands"] == b["demands"]
    # 7. peak_load on the small chain = rho_actual x (1 + |share-weighted stream sinusoid sum| / D) x worse edge trend.
    pl = base["nodes"][1]
    ph = sum(m["units_day"] * m["season_amp"] * np.exp(1j * m["season_phase"]) for m in base["demands"])
    tr = [sum(m["units_day"] * (1 + m["trend"]) ** e for m in base["demands"]) for e in (2, -2)]
    want = pl["flow_day"] * pl["mfg_time"] / pl["batch_size"] * (1 + abs(ph) / pl["flow_day"]) * max(tr) / pl["flow_day"]
    assert abs(pl["peak_load"] / want - 1) < 1e-3, (pl["peak_load"], want)
    # 8. Freight and lead times: series net cost = node carry + transport (fixed + variable) + mfg + plant purchases;
    # every site logged its order-to-receipt times; plant raw orders take ~ the link lead time.
    rows = {r["node"]: r for r in sb[900]}
    tot = sum(r["carry"] + r["transport"] + r["mfg"] + (r["node"] == "n1") * r["purchase"]
              for r in rows.values() if r["level"] is not None)
    assert abs(yb[-1, 1] / tot - 1) < 1e-9, (yb[-1, 1], tot)
    assert all(rows[f"n{v}"]["lead"] for v in (1, 2, 3)) and rows["n0"]["lead"] is None
    lt = np.mean([d for _, d in rows["n1"]["lead"]])
    assert abs(lt / base["edges"][0]["lt_mean"] - 1) < 0.2, lt
    assert mser5(np.r_[np.linspace(10, 0, 50), np.zeros(450) + np.random.default_rng(0).normal(0, 1, 450)]) >= 40
    print("chain checks passed")
