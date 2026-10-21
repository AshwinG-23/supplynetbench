# Numeric assumptions in our supply-chain dataset generator: please check them against reality

## 0. Verification status (17 Oct 2026, generator 0.7.0)

**What this dataset is.** A **partly evidence-anchored scenario generator, not an empirically calibrated
model.** A handful of inputs and outputs are now checked against dated public figures (below). Every
other number is an **uncalibrated prior**, marked as such in the tables: a rough judgement whose value
we keep until evidence arrives. Outputs are meant to be plausible and diverse, not to match any firm or
industry distribution.

**The external review (16 Oct 2026).** A third-party review checked this file against public sources.
It never saw the code, so its "risks" were hypotheses; we audited each one against the code and small
experiments. Confirmed and fixed in 0.7.0: freight was charged per order on an *expected* order size
(L4), carry was valued at transfer prices (C4), the `hold_year` floor excluded the only measured anchor
(F15), the stream phase spread flattened network seasonality (D7), several §11 counts were rounded
wrongly, and the dataset had no neutral (unstressed) stratum. Confirmed and **recorded, not changed**:
DC planning lead time is transit only (S12), plant capacity is sized at pivot-day demand (K1), the
finite-supplier store floor dominates (K6), the order-size CV floor binds in pharma (D2). Checked and
found correct: the (s, S) inventory position, the plant planning lead time, the stored order-size
moments, the margin split at DCs without tiers, the forecast-bias formulas.

**Anchors the review verified** (each is a comparator for one input or output, not a calibration):

| Anchor | Value | Used for | Source |
|---|---|---|---|
| APQC inventory carrying cost (measure 100784, n = 6,468, cross-industry, undated) | median **10.0%** of inventory value per year | `hold_year` lower end 0.10 (F15, E15, P15, I15) | [APQC](https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/inventory-carrying-cost-percentage) |
| APQC order fill rate (measure 101445, n = 3,218) | median **94.0%** of orders filled completely | Comparator for fill (§11); APQC counts orders filled completely, we count units served at once with lost sales, so not the same metric | [APQC](https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/order-fill-rate) |
| RBI OBICUS, Q1 FY2026-27 | manufacturing capacity utilisation **74.6%** (76.3% seasonally adjusted) | K1: our ρ range U(0.6, 0.97) contains it (aggregate, not a per-plant bound) | [RBI](https://www.rbi.org.in/scripts/PublicationsView.aspx?id=24087) |
| NPPA ceiling-price method | ceiling = average PTR **× 1.16**, i.e. **13.8% of price** | P13, C2 pharma trade margin (scheduled formulations) | [NPPA](https://www.nppa.gov.in/en/proactivedisclosure) |
| DMart FY2025 / Walmart US FY2026 | 415 stores / 75 DCs = **5.5 stores per DC**; 5,212 stores and clubs / 192 facilities = **27 per facility** | N4 fan-out; our echelon DC : retailer ratio 0.162 (~6 retailers per DC) sits near DMart | [DMart AR](https://www.msei.in/SX-Content/Listing/Annual-Reports/2025/DMART-2025.pdf), [Walmart 10-K](https://www.sec.gov/Archives/edgar/data/104169/000010416926000055/wmt-20260131.htm) |
| Inventory turns: Best Buy FY2026, Fastenal FY2025, DMart FY2025 | **6.3**, **2.7**, **11.6** (COGS / average inventory) | §11 turns (electronics, industrial, FMCG); company networks, not one SKU, so only an order-of-magnitude check | [Best Buy](https://investors.bestbuy.com/News--Events/news/news-details/2026/Best-Buy-Reports-Fourth-Quarter-Results/default.aspx), [Fastenal 10-K](https://www.sec.gov/Archives/edgar/data/815556/000081555626000009/fast-20251231.htm), DMart AR |
| Renesas Naka fab fire, 2021 | restart after 29 days, full production after **~97 days** | O4: our MTTR is days to weeks; a long staged-recovery tail is deferred (§12.1) | [Renesas](https://www.renesas.com/en/about/press-room/update-10-notice-regarding-semiconductor-manufacturing-factory-naka-factory-fire-production-capacity) |
| DPIIT-NCAER logistics cost | **7.97% of GDP**, FY2023-24 | L6, **comparator only** (different numerator and denominator from our transport / revenue) | [DPIIT-NCAER](https://www.dpiit.gov.in/static/uploads/2025/09/c0d7c33f0b737ed71728ff6c2a35f1e7.pdf) |

**Regimes (new in 0.7.0).** 80% of configs are the **stress** regime: the failure-rich ranges in this
file (forecast bias, price wars, low service targets). 20% are a neutral **control**: no forecast bias,
normal margins (margin factor 1) and a service target from {0.90, 0.95, 0.98} (APICS-style 90-98% goals);
per-stage and per-site noise stays. The column `regime` is in the configs and runs tables. §11 reports
both groups.

## 1. What this is and what we are asking

**The simulator.** We generate a large synthetic dataset of supply chains. It will be used to train
and test machine-learning models that predict how a network performs. Each data point is one supply
network simulated with **SupplyNetPy** (a Python discrete-event library built on SimPy) for 5 years
at a **daily time step**. The main points:

- **Single product.** Every plant makes the same product, and every site stocks only that product.
- **Network:** suppliers (S), plants or manufacturers (M), inventory sites (I: distribution centres
  and retailers), and customer demand streams (D). About 75% of networks are layered "echelon"
  graphs: suppliers, then plants, then 0-3 DC tiers, then retailers, then customers. The other 25%
  are random trees.
- **Inventory policy:** every plant and inventory site uses a **periodic-review (s, S) policy**.
  Every r days the site checks its stock and, if it is at or below s, orders up to S.
- **Customers** arrive as a Poisson process. Each takes what is on the shelf, and any unmet demand is
  lost. Customers never wait.
- **Four industry scenarios:** FMCG/grocery, electronics, pharma, industrial spare parts/MRO. Each
  scenario sets the ranges for demand, lead times, costs, prices, stock policy and service targets.
- **Plants** have finite throughput. Some suppliers have finite throughput. Suppliers, plants and
  upstream DCs can have random outages.
- Some configurations are deliberately badly managed: they under-forecast, use thin margins or set
  low service targets. We do this so the dataset contains failures as well as good practice. Since
  0.7.0, 20% of configurations are a neutral **control** regime without these stresses (§0).

**Units used throughout**
- Time: **days**. A year is 365 days.
- Quantities: **product units**. Suppliers count in raw-material units, and a plant uses 1-3 raw
  units of each input per product unit.
- Money: **currency-free price units**. Each scenario has its own scale, set by the supplier raw cost
  range. Only ratios matter: margins, cost shares, price/cost multiples. You do not need to convert
  currencies. If a real figure is in USD or INR, compare it as a ratio.
- "U(a, b)" means uniform between a and b. "LU(a, b)" means log-uniform. "LN(0, s)" means a lognormal
  multiplier with log-sd s. "N(0, s)" means normal noise. "{1, 7}" means a random choice from the set.
- Many values are drawn in two levels. A **config-level** value is drawn once per simulated network,
  and a **site-level** value adds per-site noise around it. The ranges below are the config-level
  ranges unless stated otherwise.

**What "realistic" means here.** We do not want to model one specific firm. We want the **range** of
each input to cover what real supply chains in that industry plausibly have, including struggling
firms. The **outcomes** (fill rates, margins, inventory turns, days of cover) should also fall in
ranges seen in real companies. A range that is too narrow is as much a problem as one that is wrong.

**What we would like you (the checker) to do**
- Treat every number below as **our current guess, not a fact**. Many are rough judgements with no
  source behind them, and we say so where that is true. We may simply be wrong. Please propose
  changes freely, including a complete change of a value or range.
- For each parameter, look for real-world evidence. Cite sources with links, give a confidence
  level, and propose a value or range.
- If real data varies so widely that no range can be pinned down, say so. That is a useful answer.
- If a parameter is the **wrong concept** and not just the wrong number, flag it as structurally
  wrong. Examples: a cost we derive that should be an input, or a distribution with the wrong shape.
- Note when India and global (or US/EU) values differ materially. The project is in India.

**What we would like you not to do**
- Please don't narrow everything to best practice. Some ranges deliberately reach into poor
  management (marked "deliberate failure regime" below). For those, the useful question is whether
  real firms are ever that bad, and how often.
- Please don't spend effort on the parameters in §10 (simulation design). They are statistical
  methodology, mostly not checkable against the real world. They are listed so the list is complete.
- Please don't invent numbers when there is no source. "No good evidence found" is a fine answer.

Code locations are `file:line` in `generator/` as read on 2026-10-17 (version 0.7.0). The code is being edited, so
lines may shift by a few. Parameters have IDs (N1, F3, ...) so you can refer to them in your reply.

---

## 2. Network shape (`generator/topology.py`, `generator/dataset.py`)

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why we chose it (honest) | Compare against (search terms / sources) | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| N1 | Network size n (`dataset.py:155`, `:685-686`) | n = number of nodes (customer streams included), LU(10, 150); 5% of sizes LU(150, 300) (`run_build.sh`, `--large-share 0.05 --large-max 300`) | How many nodes a network has | Compute budget and ML convenience; not from data. Real networks can be far larger. | "number of distribution centers" by company, "number of tier-1 suppliers", Gartner Supply Chain Top 25 network descriptions, 10-K "properties" sections | |
| N2 | Echelon vs random-tree share (`dataset.py:670`) | 75% echelon DAG, 25% uniform random tree | Share of realistic layered networks vs random trees kept as an out-of-distribution test | Methodology choice. A v1 review found random trees unrealistically deep (median depth 10). | Mostly methodology. Real data is only relevant to whether layered networks dominate (they likely do). | |
| N3 | Number of DC tiers k (`topology.py:27`) | P(k = 0, 1, 2, 3) = 0.2, 0.4, 0.3, 0.1 | Distribution tiers between plant and retailer: 0 = plant ships direct to retailers; 2 = e.g. national DC, then regional DC | **Uncalibrated prior.** Rough guess from "real networks have 3-5 echelons" (review note); no source recorded | "distribution network echelons", "central DC regional DC", "number of echelons" FMCG / pharma distribution (India: C&F agent, distributor, stockist, retailer), APICS/ASCM | |
| N4 | Fan-out per tier (`topology.py:28`) | LU(2, 20) per tier, drawn per graph; all fan-outs shrunk together to fit n; a DC tier is dropped if the shrunk fan-out falls below 1.5 (`topology.py:100`) | How many children each node in one tier has on average (e.g. retailers per regional DC) | **Uncalibrated prior.** Rough guess; "real DC:retailer is ~1:10+" (review note, unsourced) | "stores per distribution center" (Walmart, Reliance Retail, DMart 10-K / annual report), "retailers per distributor" India FMCG, "DCs per plant" | |
| N5 | Preferential-attachment strength a (`topology.py:122`) | LU(0.3, 3); leftover children choose a parent with weight (children so far + a). Small a gives skewed fan-out. | How unequal the sizes of parents in one tier are (some DCs serve many stores, some few) | **Uncalibrated prior.** Rough guess | "DC size distribution", "stores served per DC" variance; probably weak evidence | |
| N6 | Suppliers per plant (`topology.py:29`) | U(0.7, 2.5) per graph (below 1 means some suppliers feed several plants) | Raw-material suppliers per plant. Each supplier gives one raw material. | **Uncalibrated prior.** Rough guess. Kept small because of the single-product, 1-3-inputs simplification. | "number of suppliers per plant", bill-of-materials size by industry; structurally it is "number of distinct inputs" | |
| N7 | Multi-sourcing probability (`topology.py:30`) | P = U(0.1, 0.3) per graph that a site gets a 2nd parent in the same tier | Share of sites supplied by two DCs or plants (dual sourcing) | **Uncalibrated prior.** Rough guess (review suggestion) | "dual sourcing prevalence", "percentage of SKUs dual sourced", "multi-sourcing distribution centers" | |
| N8 | Tier-skip probability (`topology.py:31`) | P = U(0, 0.08) per graph that a site at tier ≥ 2 also gets a parent ≥ 2 tiers up (e.g. plant to retailer direct) | Direct-store-delivery / bypass flows | **Uncalibrated prior.** Rough guess | "direct store delivery share" (beverages, bakery), "DSD percentage of volume" | |
| N9 | Shared supplier probability (`topology.py:32`) | P = U(0.05, 0.3) that a plant gets an extra supplier already feeding another plant | Suppliers that serve several plants | **Uncalibrated prior.** Rough guess | "supplier concentration", "shared suppliers across plants" | |
| N10 | DC with own customers (`topology.py:33`) | P = U(0, 0.15) per DC | DCs that also sell to end customers (e.g. cash-and-carry, hospital supply from a DC) | **Uncalibrated prior.** Rough guess | "DC serves end customers directly", omnichannel fulfilment from DC | |
| N11 | One customer stream per retailer (`topology.py:95`, `:133-136`) | Exactly 1 demand stream per retailer | Each retailer faces one aggregated customer stream | Structural simplification | Structural; no number to check | |
| N12 | Random-tree node shares (`topology.py:145-148`) | suppliers 5-15% of n, plants 3-10%, customer-facing sites / inventory sites ratio drawn as 1/(1+U(0.3, 0.9)) | Proportions of node types in the 25% random-tree stratum | **Uncalibrated prior.** Rough guess ("realistic proportions" in code comment, no source) | Same sources as N3-N6 | |
| N13 | Graphs per size, distinct (`run_build.sh`) | Up to 50 distinct graphs per size, repeats rejected | Dataset design | Methodology | Not checkable | |

Measured network shape in the 400-run check build (§11) is there to sanity-check N3-N6.

---

## 3. Industry scenarios (`generator/chain.py:56-85`, `SCENARIOS`)

Each scenario sets the ranges below. The code comments give the only rationale recorded: FMCG "high
volume, short legs, thin margin"; electronics "high value, long inbound legs, obsolescence-heavy
holding cost"; pharma "high margin, high service target, long API lead times"; industrial "spare
parts / MRO: slow, lumpy demand, expensive items". The generator notes say the ranges are
"design judgements sanity-checked against published figures, not calibrated to a firm". **No
specific sources were recorded for any scenario number** except the 0.10 lower end of `hold_year` (APQC,
§0). Every other value is an **uncalibrated prior**: a rough judgement, kept as it is until evidence arrives.

### 3.0 Field glossary (same fields in every industry)

| Field (code key) | Meaning | Drawn as |
|---|---|---|
| `units_day` (median, σ) | Mean customer demand per retailer stream, units/day | Lognormal per stream with that median and log-sd, clamped 0.05-2000 (`chain.py:225`) |
| `arrivals_day` | Customer orders per day per stream | U(lo, hi), capped at the mean units/day so the mean order size is ≥ 1 (`chain.py:226`) |
| `qty_cv` | Coefficient of variation of customer order size | U(lo, hi); order size = 1 + negative binomial (Poisson floor) with that mean and CV (`chain.py:116-124, 228`) |
| `lt` SM / MI / II | Mean transport lead time (days) on supplier→plant, plant→DC/retailer, DC→DC/retailer links | U(lo, hi) per link (`chain.py:205`) |
| `lt_cv` | Lead-time variability (CV of each shipment's gamma-distributed lead time) | Config value U(lo, hi), then × U(0.5, 1.5) per link (`chain.py:186, 205`) |
| `raw_cost` | Price per raw-material unit at a supplier (sets the money scale) | U(lo, hi) per supplier (`chain.py:265`) |
| `recipe` | Raw units of each input per product unit | Integer U{1..3} per plant×input (`chain.py:244`) |
| `mfg_ratio` | Manufacturing (conversion) cost / raw-material cost per unit | U(lo, hi) per plant (`chain.py:268`) |
| `mfg_time` | Days to produce one batch | U(lo, hi) per plant (`chain.py:245`) |
| `gross` | Retail price / plant unit cost over the whole path, before the margin factor (C1) | U(lo, hi) per config |
| `plant_share` | Part of log(gross) earned by the plant (the rest goes to DCs and retail) | U(lo, hi) per config |
| `hold_year` | Annual inventory holding cost as a share of unit **cost** (consolidated cost basis, C4) | U(lo, hi) per config |
| `cycle` M / I / R | Reference order cycle (days between orders) for a median-flow plant / DC / retailer; scaled by EOQ √(median flow / flow) | U(lo, hi) per stage, then per site, clipped to this range |
| `review` M / I / R | Review period (days) for plant / DC / retailer | Random choice from the set |
| `service` | Config mean cycle-service target (probability of no stockout per cycle) | U(lo, hi) per config |
| `freight` | Variable freight as a share of the value received, per leg (charged at receipt, L4) | U(lo, hi) per config |
| `unit` | What one product unit is (label only, configs.unit): FMCG "consumer pack", electronics "device", pharma "strip / pack", industrial "part" | Fixed per scenario |

### 3.1 FMCG / grocery (`chain.py:57-63`)

| ID | Field (line) | Current value | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|
| F1 | units_day median (`:58`) | 40 units/day per retailer stream | **Uncalibrated prior.** Rough guess, "high volume" | Store-level unit sales per SKU per day for fast movers; Nielsen/NielsenIQ, Kantar; India kirana vs modern-trade sales per SKU | |
| F2 | units_day σ (`:58`) | 0.8 (lognormal: p10 ≈ 14, p90 ≈ 112) | **Uncalibrated prior.** Rough guess | Spread of store sales across stores (Pareto/80-20 of store sizes) | |
| F3 | arrivals_day (`:58`) | U(2, 6) orders/day | **Uncalibrated prior.** Rough guess. Note: with 40 units/day this gives ~7-20 units per "order", so an order is more a basket or batch than one shopper. | Is a customer "order" here one shopper (then arrivals should be far higher) or a replenishment drop? Flag if structurally odd. | |
| F4 | qty_cv (`:58`) | U(0.3, 0.8) | **Uncalibrated prior.** Rough guess | CV of daily SKU-store demand in grocery (M5 / Walmart Kaggle dataset, Favorita dataset) | |
| F5 | lt SM (`:59`) | U(2, 10) days | **Uncalibrated prior.** Rough guess, "short legs" | Packaging / ingredient supplier lead times FMCG; India FMCG procurement lead times | |
| F6 | lt MI (`:59`) | U(1, 5) days | **Uncalibrated prior.** Rough guess | Plant to DC transit time, FMCG; India road freight transit times | |
| F7 | lt II (`:59`) | U(1, 3) days | **Uncalibrated prior.** Rough guess | DC to store replenishment lead time grocery (often 1-2 days) | |
| F8 | lt_cv (`:59`) | U(0.0, 0.3) | **Uncalibrated prior.** Rough guess | On-time-in-full (OTIF) rates, transit time variability, India trucking reliability | |
| F9 | raw_cost (`:60`) | U(0.2, 2.0) | Sets money scale only; ratio to other costs matters | Not meaningful alone | |
| F10 | recipe (`:60`) | 1-3 units per input | Structural simplification | Structural | |
| F11 | mfg_ratio (`:60`) | U(0.3, 1.0) | **Uncalibrated prior.** Rough guess | FMCG COGS breakdown: materials vs conversion cost (10-K / annual reports: HUL, Nestlé India, P&G); "conversion cost as % of material cost" | |
| F12 | mfg_time (`:60`) | U(0.5, 2) days per batch | **Uncalibrated prior.** Rough guess | Production batch / campaign length FMCG | |
| F13 | gross (`:61`) | U(1.5, 2.4) (retail price / plant unit cost) | **Uncalibrated prior.** Rough guess, "thin margin" | Shelf price / factory COGS for packaged goods: manufacturer gross margin (Damodaran "Food Processing", "Household Products") combined with retailer gross margin (Damodaran "Retail (Grocery and Food)"); India MRP vs ex-factory price, distributor and retailer margins | |
| F14 | plant_share (`:61`) | U(0.25, 0.5) of log margin goes to the plant | **Uncalibrated prior.** Rough guess | Split of total markup between manufacturer, distributor, retailer (India FMCG: distributor ~5-8%, retailer ~10-20% of MRP are commonly quoted, please verify) | |
| F15 | hold_year (`:62`) | U(0.10, 0.30) per year (0.6.0: U(0.20, 0.30)) | Lower end 0.10 = APQC median carrying cost (anchor, §0); upper end **uncalibrated prior** kept as a sensitivity range (was the textbook 20-30%) | "inventory carrying cost percentage" APICS/ASCM, cost of capital India vs US | |
| F16 | cycle M (`:62`) | U(7, 21) days | **Uncalibrated prior.** Rough guess | Production / replenishment frequency for plants | |
| F17 | cycle I (`:62`) | U(3, 14) days | **Uncalibrated prior.** Rough guess | DC replenishment order frequency | |
| F18 | cycle R (`:62`) | U(1, 7) days | **Uncalibrated prior.** Rough guess | Store delivery frequency in grocery (daily to weekly); India distributor visit frequency to kiranas | |
| F19 | review M (`:63`) | {1, 7} days | **Uncalibrated prior.** Rough guess | Plant planning cycle (daily vs weekly MRP runs) | |
| F20 | review I (`:63`) | 1 day | **Uncalibrated prior.** Rough guess | DC replenishment review frequency | |
| F21 | review R (`:63`) | 1 day | **Uncalibrated prior.** Rough guess | Store replenishment review (daily automated ordering) | |
| F22 | service (`:63`) | config mean U(0.50, 0.99); control regime (20% of configs): {0.90, 0.95, 0.98} | Upper end typical target; lower end **deliberate failure regime** | Target vs achieved on-shelf availability; ECR / GMA out-of-stock studies (often ~8% OOS quoted), case fill rate benchmarks | |
| F23 | freight (`:63`) | U(0.01, 0.03) of value per leg | **Uncalibrated prior.** Rough guess. The code comment used to say transport ~1-4% of sales; it now quotes the measured value (§11). Since 0.7.0 this share is charged on the value actually received (L4) | "freight cost as percent of sales" FMCG, CSCMP State of Logistics, India logistics cost as % of sales (FMCG) | |

### 3.2 Electronics (`chain.py:64-70`)

| ID | Field (line) | Current value | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|
| E1 | units_day median (`:65`) | 8 units/day | **Uncalibrated prior.** Rough guess | Store-level unit sales of a consumer-electronics SKU (phones, accessories) | |
| E2 | units_day σ (`:65`) | 0.9 | **Uncalibrated prior.** Rough guess | Spread across stores | |
| E3 | arrivals_day (`:65`) | U(0.5, 2) | **Uncalibrated prior.** Rough guess | Orders/day per store for one SKU | |
| E4 | qty_cv (`:65`) | U(0.3, 1.0) | **Uncalibrated prior.** Rough guess | Demand CV consumer electronics | |
| E5 | lt SM (`:66`) | U(14, 45) days | **Uncalibrated prior.** Rough guess, "long inbound legs" | Component lead times: semiconductor lead-time reports (Susquehanna chip lead times, often 10-26 weeks in shortages), ocean freight Asia→India transit | |
| E6 | lt MI (`:66`) | U(5, 30) days | **Uncalibrated prior.** Rough guess | Factory (China/Vietnam/India EMS) to regional DC transit (ocean vs air) | |
| E7 | lt II (`:66`) | U(2, 7) days | **Uncalibrated prior.** Rough guess | DC to store | |
| E8 | lt_cv (`:66`) | U(0.1, 0.5) | **Uncalibrated prior.** Rough guess | Ocean schedule reliability (Sea-Intelligence GLP), customs delays | |
| E9 | raw_cost (`:67`) | U(10, 120) | Money scale | Not meaningful alone | |
| E10 | recipe (`:67`) | 1-3 | Structural | Structural (real BOMs have hundreds of parts) | |
| E11 | mfg_ratio (`:67`) | U(0.2, 0.6) | **Uncalibrated prior.** Rough guess | Assembly/test cost vs component cost (teardown reports: TechInsights, Counterpoint BOM analyses) | |
| E12 | mfg_time (`:67`) | U(2, 7) days | **Uncalibrated prior.** Rough guess | Manufacturing cycle time electronics assembly | |
| E13 | gross (`:68`) | U(1.3, 2.0) | **Uncalibrated prior.** Rough guess, thin. Produces most of the loss-making runs (§11). | Retail price / BOM+assembly (teardown-based "BOM as % of retail price"), Damodaran "Electronics (Consumer & Office)", retailer margins (Best Buy, Croma, Reliance Digital) | |
| E14 | plant_share (`:68`) | U(0.55, 0.85) | **Uncalibrated prior.** Rough guess (brand/OEM captures most) | OEM vs retailer share of margin in electronics | |
| E15 | hold_year (`:69`) | U(0.10, 0.40) (0.6.0: U(0.25, 0.40)) | Lower end 0.10 = APQC median (§0); upper end **uncalibrated prior**, "obsolescence-heavy" | Carrying cost including obsolescence/price erosion for electronics | |
| E16 | cycle M (`:69`) | U(14, 45) | **Uncalibrated prior.** Rough guess | | |
| E17 | cycle I (`:69`) | U(14, 30) | **Uncalibrated prior.** Rough guess | | |
| E18 | cycle R (`:69`) | U(7, 14) | **Uncalibrated prior.** Rough guess | Store replenishment frequency electronics | |
| E19 | review M (`:70`) | 7 days | **Uncalibrated prior.** Rough guess | Weekly planning | |
| E20 | review I (`:70`) | {1, 7} | **Uncalibrated prior.** Rough guess | | |
| E21 | review R (`:70`) | 1 | **Uncalibrated prior.** Rough guess | | |
| E22 | service (`:70`) | U(0.50, 0.98); control regime (20% of configs): {0.90, 0.95, 0.98} | Lower end **deliberate failure regime** | In-stock rates consumer electronics retail | |
| E23 | freight (`:70`) | U(0.005, 0.02) per leg | **Uncalibrated prior.** Rough guess (high value per unit → low share) | Logistics cost % of sales, electronics | |

### 3.3 Pharma (`chain.py:71-77`)

| ID | Field (line) | Current value | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|
| P1 | units_day median (`:72`) | 15 units/day | **Uncalibrated prior.** Rough guess | Pharmacy dispensing per SKU per day; India retail chemist sales (AIOCD-AWACS) | |
| P2 | units_day σ (`:72`) | 0.8 | **Uncalibrated prior.** Rough guess | | |
| P3 | arrivals_day (`:72`) | U(1, 3) | **Uncalibrated prior.** Rough guess | | |
| P4 | qty_cv (`:72`) | U(0.2, 0.6) | **Uncalibrated prior.** Rough guess ("chronic meds are stable") The order-size floor raises the realised CV in 34% of pharma streams (D2). | Demand variability in pharmacy SKUs | |
| P5 | lt SM (`:73`) | U(30, 90) days | **Uncalibrated prior.** Rough guess, "long API lead times" | API procurement lead times (India imports of APIs from China), excipient lead times | |
| P6 | lt MI (`:73`) | U(3, 10) days | **Uncalibrated prior.** Rough guess. Does not include QC release / batch testing time, which may be much longer. | Plant to C&F/distributor transit; QA release time (often weeks) | |
| P7 | lt II (`:73`) | U(1, 4) days | **Uncalibrated prior.** Rough guess | Distributor/stockist to pharmacy delivery (India often same/next day) | |
| P8 | lt_cv (`:73`) | U(0.05, 0.3) | **Uncalibrated prior.** Rough guess | | |
| P9 | raw_cost (`:74`) | U(2, 40) | Money scale | Not meaningful alone | |
| P10 | recipe (`:74`) | 1-3 | Structural | | |
| P11 | mfg_ratio (`:74`) | U(0.5, 2.0) | **Uncalibrated prior.** Rough guess (conversion, QC, compliance cost can exceed material) | Pharma COGS breakdown (API vs formulation conversion) | |
| P12 | mfg_time (`:74`) | U(3, 10) days per batch | **Uncalibrated prior.** Rough guess | Pharma batch manufacturing cycle time (excluding QC release) | |
| P13 | gross (`:75`) | U(1.8, 4.5) | **Uncalibrated prior.** Rough guess, "high margin". Generics (thin) and branded (fat) are mixed in one range. | Damodaran "Drugs (Pharmaceutical)" and "Drugs (Generic)", India: NPPA ceiling price = average PTR × 1.16 for scheduled formulations, i.e. the retailer's 16% markup on PTR is **13.8% of the ceiling price** (§0); the 8% stockist margin is trade convention, not a universal rule. MRP vs cost | |
| P14 | plant_share (`:75`) | U(0.6, 0.85) | **Uncalibrated prior.** Rough guess | Manufacturer vs trade share of margin | |
| P15 | hold_year (`:76`) | U(0.10, 0.30) (0.6.0: U(0.20, 0.30)) | Lower end 0.10 = APQC median (§0); upper end **uncalibrated prior** (textbook) | Pharma carrying cost incl. expiry write-offs, cold chain | |
| P16 | cycle M (`:76`) | U(30, 60) | **Uncalibrated prior.** Rough guess | Campaign / batch frequency | |
| P17 | cycle I (`:76`) | U(14, 30) | **Uncalibrated prior.** Rough guess | | |
| P18 | cycle R (`:76`) | U(7, 14) | **Uncalibrated prior.** Rough guess. Indian pharmacies often order daily from stockists. | | |
| P19 | review M (`:77`) | 7 | **Uncalibrated prior.** Rough guess | | |
| P20 | review I (`:77`) | {1, 7} | **Uncalibrated prior.** Rough guess | | |
| P21 | review R (`:77`) | 1 | **Uncalibrated prior.** Rough guess | | |
| P22 | service (`:77`) | U(0.70, 0.995); control regime (20% of configs): {0.90, 0.95, 0.98} | **Uncalibrated prior.** Rough guess, "high service target" | Pharmacy fill rate / drug availability; FDA drug shortage data; India public-sector drug availability surveys (WHO/HAI) | |
| P23 | freight (`:77`) | U(0.003, 0.01) per leg | **Uncalibrated prior.** Rough guess | Pharma logistics cost % of sales (incl. cold chain) | |

### 3.4 Industrial / spare parts / MRO (`chain.py:78-84`)

| ID | Field (line) | Current value | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|
| I1 | units_day median (`:79`) | 2 units/day | **Uncalibrated prior.** Rough guess, "slow" | Spare parts demand per SKU per location (most spare parts sell < 1/day) | |
| I2 | units_day σ (`:79`) | 1.0 | **Uncalibrated prior.** Rough guess | | |
| I3 | arrivals_day (`:79`) | U(0.1, 0.8) | **Uncalibrated prior.** Rough guess, "lumpy" | Intermittent-demand literature (Syntetos-Boylan ADI > 1.32 classification), spare parts order frequency | |
| I4 | qty_cv (`:79`) | U(1.0, 2.0) | **Uncalibrated prior.** Rough guess, "lumpy" | CV² of non-zero demand sizes in spare parts (Syntetos-Boylan cut-off 0.49) | |
| I5 | lt SM (`:80`) | U(7, 30) | **Uncalibrated prior.** Rough guess | Industrial component / casting / forging lead times (can be months) | |
| I6 | lt MI (`:80`) | U(3, 14) | **Uncalibrated prior.** Rough guess | | |
| I7 | lt II (`:80`) | U(2, 7) | **Uncalibrated prior.** Rough guess | Distributor to customer / branch | |
| I8 | lt_cv (`:80`) | U(0.1, 0.4) | **Uncalibrated prior.** Rough guess | | |
| I9 | raw_cost (`:81`) | U(20, 400) | Money scale, "expensive items" | Not meaningful alone | |
| I10 | recipe (`:81`) | 1-3 | Structural | | |
| I11 | mfg_ratio (`:81`) | U(0.3, 1.0) | **Uncalibrated prior.** Rough guess | Machining/assembly vs material cost | |
| I12 | mfg_time (`:81`) | U(2, 10) days | **Uncalibrated prior.** Rough guess | | |
| I13 | gross (`:82`) | U(1.6, 3.5) | **Uncalibrated prior.** Rough guess | Spare-parts margins (aftermarket often high), Damodaran "Machinery", industrial distributor gross margins (Grainger, Fastenal 10-K) | |
| I14 | plant_share (`:82`) | U(0.4, 0.7) | **Uncalibrated prior.** Rough guess | | |
| I15 | hold_year (`:83`) | U(0.10, 0.35) (0.6.0: U(0.20, 0.35)) | Lower end 0.10 = APQC median (§0); upper end **uncalibrated prior** (textbook + obsolescence) | | |
| I16 | cycle M (`:83`) | U(30, 60) | **Uncalibrated prior.** Rough guess | | |
| I17 | cycle I (`:83`) | U(14, 60) | **Uncalibrated prior.** Rough guess | | |
| I18 | cycle R (`:83`) | U(14, 30) | **Uncalibrated prior.** Rough guess | | |
| I19 | review M (`:84`) | 7 | **Uncalibrated prior.** Rough guess | | |
| I20 | review I (`:84`) | 7 | **Uncalibrated prior.** Rough guess | | |
| I21 | review R (`:84`) | {1, 7} | **Uncalibrated prior.** Rough guess | | |
| I22 | service (`:84`) | U(0.50, 0.98); control regime (20% of configs): {0.90, 0.95, 0.98} | Lower end **deliberate failure regime** | Spare parts fill-rate benchmarks (often 90-95% targeted, verify) | |
| I23 | freight (`:84`) | U(0.01, 0.03) per leg | **Uncalibrated prior.** Rough guess | Logistics cost % of sales, industrial distribution | |

---

## 4. Demand (global constants, `generator/chain.py`)

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| D1 | Demand clamp (`chain.py:225`) | Mean units/day per stream clamped to [0.05, 2000] | Safety bounds on the lognormal draw | Numerical guard, not a real-world claim | Not checkable | |
| D2 | Order-size model (`chain.py:116-124, 228`) | 1 + negative binomial with exact mean and variance; variance at least the Poisson floor | Shape of customer order-size distribution | Chosen for exact moments; shape not checked against data. **Note (0.7.0 audit):** the 1 + NB form forces CV ≥ √(q−1)/q, so the realised order-size CV is above the drawn `qty_cv` when that is lower: the floor binds in 34% of pharma, 11% of electronics, 9% of FMCG and 0% of industrial streams (+0.05-0.09 CV when it binds; pharma realised CV p10/50/90 0.30/0.44/0.56). `demands.qty_var` stores the realised variance. | Is negative binomial a reasonable order-size shape? (retail basket quantity distributions, spare parts order sizes) | |
| D3 | Customer arrivals (`chain.py:127-144`) | Non-homogeneous Poisson process | Customers arrive independently at random | Standard modelling choice | Structural; flag if clearly wrong | |
| D4 | P(no seasonality) (`chain.py:102`) | 0.3 per config | 30% of networks have flat demand through the year | **Uncalibrated prior.** Rough guess | Share of products with meaningful annual seasonality, by industry | |
| D5 | Seasonal amplitude (`chain.py:102`, `:190`) | Config amp U(0, 0.5); per stream × U(0.5, 1.5) (`:232`), so at most 0.75 (the old 0.9 cap never bound and was removed, no behaviour change) | Demand = base × (1 + amp × sin(annual)); amp 0.5 means peak is 3× trough | **Uncalibrated prior.** Rough guess | Seasonal indices by category (peak/trough ratio): festive season India (Diwali) for electronics, monsoon for pharma (anti-infectives), summer beverages | |
| D6 | Seasonal phase (`chain.py:190`) | U(0, 2π) per config | Which month the peak falls in | Arbitrary (no calendar) | Not really checkable; only relevant if the checker thinks peaks should align with real months | |
| D7 | Seasonal phase spread across streams (`chain.py:191`, `:233`) | Per stream phase = config phase + N(0, U(0, π/4)) (0.6.0: U(0, π)) | **Uncalibrated prior.** Cut in 0.7.0: with spread up to π the network-level seasonal amplitude (demand-weighted vector sum of the streams' sinusoids) was a median 0.53× the config amplitude, and 46% of seasonal configs lost over half of it. A spread ≤ π/4 (~1.5 months) keeps ≥ ~0.74×; measured after the change in §11.4 | **Uncalibrated prior.** Rough guess; with spread up to π, streams can be opposite in phase | Do different stores of one product peak at different times? (regional climate/festival differences) | |
| D8 | Seasonal shape | Pure annual sinusoid, period 365 d (`chain.py:131`) | One smooth peak per year | Simplification | Structural: real seasonality has spikes (festivals, promotions), weekly patterns | |
| D9 | Annual trend, config mean (`chain.py:103`) | U(−0.10, +0.20) per year | Demand grows or shrinks steadily over the 5 years | **Uncalibrated prior.** Rough guess | Category growth rates: FMCG India volume growth, electronics, pharma market growth (IQVIA, IPA), industrial | |
| D10 | Trend noise per stream (`chain.py:234`) | + N(0, 0.03) | Retailer-to-retailer growth differences | **Uncalibrated prior.** Rough guess | Like-for-like store sales growth dispersion | |
| D11 | Trend/season not anticipated (`chain.py:24-25`) | Planners size stock on the base rate only | No forecasting of seasonality or growth | Deliberate simplification, makes runs harder | Structural; real planners forecast seasonality | |
| D12 | Lost sales (`chain.py:469`) | Unmet customer demand is lost (no backorders) | Customers walk away | Simplification. SupplyNetPy waiting-customer logic had bugs. | Lost-sale vs backorder share by industry (retail ~lost; industrial/B2B often backordered; ECR OOS studies on substitution) | |
| D13 | Independence across streams | Each customer stream has its own random stream; no common shocks | Retailers' demand is uncorrelated apart from shared trend and season | Simplification | Cross-store demand correlation | |

---

## 5. Inventory policy and planning error (`generator/chain.py`)

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| S1 | Policy type (`chain.py:13-14, 307-320`) | Periodic-review (s, S) at every plant and DC/retailer; s = D̂(L+r) + z·σ; S = s + Q | Textbook reorder point / order-up-to | Standard textbook policy | Structural: is (s,S) a fair stand-in for MRP/DRP practice? | |
| S2 | Forecast bias, config mean `BIAS` (`chain.py:96`) | log(forecast/true) ~ U(−0.7, +0.15) per config, so forecast between 0.50× and 1.16× true demand | Systematic under- or over-forecasting | **Uncalibrated prior, tuned to get failures, not from data**: widened after the pilot so ~15% of runs have fill < 0.7. −0.7 (forecast is half of real demand) is extreme. Stress regime only: the 20% control regime sets the bias to 0 (§0, `chain.py:91`, `:193-196`). | Forecast bias benchmarks (IBF Institute of Business Forecasting, Gartner forecast accuracy, "forecast bias percentage" by industry); does persistent 50% under-forecasting happen? | |
| S3 | Forecast error spread per site `BIAS_SD` (`chain.py:97`) | Log-sd U(0.05, 0.3) per config | Site-to-site differences in forecast error | **Uncalibrated prior.** Rough guess | MAPE / forecast error at SKU-location level by industry (IBF benchmarks, often 30-50% MAPE at SKU-store) | |
| S4 | Service target mean (`chain.py:63,64,71,78`) | See F22/E22/P22/I22 | Config mean cycle-service level | See scenario tables | | |
| S5 | z deviation, stage and site `Z_SD` (`chain.py:95`, `:197`, `:308`) | Stage N(0, 0.3) + site N(0, 0.3) added to z | Sites in one network don't all target the same service | **Uncalibrated prior.** Rough guess | Variation of safety-stock policy across a firm's sites | |
| S6 | z clip (`chain.py:308`) | z clipped to [−1, 3.3] (service 16%-99.95%) | Bounds on safety factor | Numerical guard | Not checkable | |
| S7 | Order cycle (EOQ form) (`chain.py:14, 305-307`) | T = T_ref·√(median flow / flow) × LN(0, 0.25), clipped to the scenario cycle range, ≥ review | Bigger sites order more often | EOQ logic; T_ref ranges are rough guesses (F16-18 etc.) | Order frequency vs volume in practice | |
| S8 | Cycle noise `CYCLE_SD` (`chain.py:98`) | Lognormal log-sd 0.25 | Sites deviate from EOQ | **Uncalibrated prior.** Rough guess | Not really checkable | |
| S9 | First review offset (`chain.py:317`) | U(0, r) | Sites don't all review on the same day | Avoids artificial synchrony | Not checkable | |
| S10 | Upstream reorder floor (`chain.py:312-313`) | s ≥ largest order quantity of any child DC/retailer | Upstream site can always fill one child order | Structural guard: SupplyNetPy ships orders whole | Structural | |
| S11 | Initial stock (`chain.py:320`) | Every site starts at S (full) | Start-up condition; removed by the warm-up | Methodology | Not checkable | |
| S12 | Lead time used for planning (`chain.py:294-300`) | Plant: E[max inbound lead time] (512 Monte Carlo draws) + batch time; DC: share-weighted mixture of link lead times. **Planning L is transit only** at DCs: a wait at a short supplier is endogenous and not planned for (deliberate, naive planners). The realised order-to-receipt time is recorded per site since 0.7.0 (`node_results.lead_time_obs`, `lead_time_obs_p90`; plants: raw orders) and reported in §11.4 | What lead time planners assume | Method | Not checkable | |
| S13 | Main supplier share `MAIN_SHARE` (`chain.py:99`, `:213-215`) | Main parent weight U(0.5, 0.9); remaining parents split Dirichlet(1) | Order split at multi-sourced sites (e.g. 70/30) | **Uncalibrated prior.** Rough guess | Typical dual-sourcing splits (70/30, 80/20 rule of thumb) | |

---

## 6. Lead times and transport (`generator/chain.py`)

Mean lead times per leg and their CV are in the scenario tables (F5-F8, E5-E8, P5-P8, I5-I8) and freight shares in F23/E23/P23/I23.

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| L1 | Lead-time distribution (`chain.py:108-113`, `:462`) | Gamma per shipment with the link's mean and CV | Shape of transit time | Common choice; not checked | Transit-time distributions (often right-skewed with long tail; lognormal also used) | |
| L2 | Per-link CV spread (`chain.py:205`) | Link CV = config CV × U(0.5, 1.5) | Some lanes more reliable than others | **Uncalibrated prior.** Rough guess | Lane-level reliability dispersion | |
| L3 | Freight per order: fixed part (`chain.py:15-16, 304, 349-352`) | K = h·c·D·T_eoq²/2 per order, c = consolidated cost basis (C4), h = holding rate per day. K is **derived** so that our chosen cycle T_eoq is the EOQ optimum: an **EOQ-implied order cost**, not measured freight. A plant's K is split over its raw links. This is the whole link cost (× U(0.8, 1.2)). | Fixed cost per order/shipment | **Structural choice, not data**: K is backed out from holding cost and an assumed cycle, not taken from real ordering/truck costs | Real fixed order/shipment cost (truckload rate, ordering admin cost); may be better as an input | |
| L4 | Freight: variable part (`chain.py:522, 537`) | freight_share × value of the units **received** (DC: units × buy price; plant: raw units × raw price), charged at receipt. 0.6.0 charged freight_share × value × E[order] per order placed, so a large catch-up order paid the same as a normal one (actual orders were 1.085× E[order] at the median, 1.6× at p90) | Value-proportional freight | Simplification (freight in reality is per weight/volume/distance) | Freight cost structure (per kg-km, per pallet); value-density by industry | |
| L5 | Freight noise per link (`chain.py:352`) | Fixed part × U(0.8, 1.2) per link; the variable part has no lane noise since 0.7.0 | Lane cost differences | **Uncalibrated prior.** Rough guess | Lane rate dispersion | |
| L6 | Freight legs compound | Each leg charges its share; a 3-leg path pays ~3× | Total logistics cost grows with depth | Intended. The 0.6.0 comment said total transport ~1-4% of sales. **Measured 2-8% (median 4.0%)** in 0.6.0; 0.7.0 values in §11. | Total logistics / transport cost as % of sales by industry. India logistics cost **7.97% of GDP, FY2023-24** (DPIIT-NCAER 2025, §0): a **comparator only**, with a different numerator (all logistics, not just freight) and denominator (GDP, not sales). The old 13-14% of GDP figure is withdrawn | |
| L7 | Shipments in transit during outages | Already-shipped orders still arrive | SupplyNetPy semantics | Simulator behaviour | Structural | |

---

## 7. Costs, prices and margins (`generator/chain.py`)

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| C1 | Margin factor `MARGIN_FACTOR` (`chain.py:92`, `:253`) | f ~ U(0.03, 1.2); effective gross G = 1 + f·(gross − 1) | Scales the whole markup: f = 0.03 means retail price ≈ cost (price war), f = 1.2 means 20% above normal | **Uncalibrated prior, tuned to get loss-making runs, not from data.** Pilot target: ~5-7% of runs loss-making. Stress regime only: the control regime sets f = 1. | Share of firms / product lines with negative operating margin by industry (Damodaran distribution of margins), price-war episodes | |
| C2 | Retail share of downstream margin `RETAIL_SHARE` (`chain.py:94`) | U(0.55, 0.85) of the non-plant log-margin goes to the last site; the rest split evenly over DC tiers | Retailer earns more than wholesalers | **Uncalibrated prior.** Rough guess | Distributor vs retailer margin split (India FMCG: distributor ~4-8%, retailer ~8-20% of MRP, please verify; US wholesale vs retail gross margins); pharma: NPPA ceiling = PTR × 1.16, so retail margin 13.8% of price on scheduled formulations, 8% stockist is convention (§0) | |
| C3 | Per-site price noise (`chain.py:263`) | Each site's markup × LN(0, 0.15) | Prices vary site to site | **Uncalibrated prior.** Rough guess | Price dispersion across stores/distributors | |
| C4 | Holding cost basis (`chain.py:271, 278`) | holding/day = hold_year/365 × consolidated cost basis (plant: unit cost; DC/retailer: share-weighted plant unit cost reaching it). 0.6.0 used the DC's buy (transfer) price, which overstated carry by the internal markups | Carrying cost per unit per day, in consolidated network terms | Network profit is consolidated (internal sales excluded), so stock is valued at cost | Carrying-cost composition (capital, storage, obsolescence, insurance) | |
| C5 | No holding cost at suppliers; supplier "mining" cost 0 (`chain.py:428-429`) | Finite suppliers' stores cost nothing to hold; raw material cost is paid by the plant at purchase. Suppliers are **outside the economic boundary**: raw price = the plant's procurement cost, and their stock is excluded from network inventory KPIs | Supplier economics ignored | Simplification, not documented elsewhere | Structural | |
| C6 | Plant unit cost (`chain.py:267-269`) | unit cost = Σ recipe × raw cost + mfg cost (raw × mfg_ratio) | Cost of goods at the plant | Simplification (no labour/overhead split, no economies of scale) | | |
| C7 | No fixed costs / overheads | Profit = customer revenue − (raw purchases + manufacturing + holding + transport). No SG&A, rent, labour at DCs/stores, R&D, marketing, tax. | Our "margin" is a contribution margin, not net margin | Simplification Metric definitions: §11.0 | Compare our margin (§11) against **contribution or gross margin**, not net margin | |
| C8 | No price elasticity | Prices never change and do not affect demand | | Simplification | Structural | |

---

## 8. Capacity (`generator/chain.py`)

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| K1 | Plant utilisation `RHO` (`chain.py:100`) | Config mean U(0.6, 0.97) | Plant runs at this share of its capacity at base demand. Throughput = demand/ρ. | **Uncalibrated prior** (review suggestion). Contains the RBI anchor (mean of U(0.6, 0.97) = 0.785). Capacity is sized on **pivot-day base demand** and kept that way as deliberate stress; seasonal peaks and trend at the window edges push the load above 1 at many plants, recorded since 0.7.0 as `nodes.peak_load` (§11.4) | Capacity utilisation by industry: Federal Reserve G.17 capacity utilization (US), RBI OBICUS survey (India: **74.6% in Q1 FY2026-27, 76.3% seasonally adjusted**, aggregate anchor, §0) | |
| K2 | Per-plant utilisation noise (`chain.py:321`) | + N(0, 0.05), clipped to [0.4, 0.99] | Plants in one network differ | **Uncalibrated prior.** Rough guess | | |
| K3 | Plant batch (`chain.py:19, 323`) | batch = ⌈D·mfg_time/ρ⌉, one batch at a time | Capacity modelled as a fixed batch per mfg_time | Simplification of a plant | Structural: no shifts, changeovers, multiple lines | |
| K4 | P(config has finite suppliers) `FINITE_SUPPLY[0]` (`chain.py:101`) | 0.5 | Half of networks have capacity-limited suppliers | **Uncalibrated prior.** Rough guess | Share of supply chains where supplier capacity binds | |
| K5 | Share of suppliers finite `FINITE_SUPPLY[1]` (`chain.py:101`) | U(0.2, 1.0) | Of those networks, this share of suppliers are finite | **Uncalibrated prior.** Rough guess | | |
| K6 | Finite supplier store `FINITE_SUPPLY[2]` (`chain.py:101`, `:336-338`) | U(7, 30) days of flow, but at least 2× the plants' order-up-to raw needs | Supplier finished-goods buffer | **Uncalibrated prior** for the U(7, 30) days. The floor is a simulator guard (an order larger than the store would never ship) and is **kept at 2×** deliberately. Measured (0.6.0 audit): the floor overrides the day draw at 97% of finite suppliers; realised store p50 132 d, p90 298 d, max 532 d, so the U(7, 30) draw is effectively dead | Supplier finished-goods inventory days (supplier 10-K DIO) | |
| K7 | Finite supplier rate (`chain.py:337`) | Rate = flow / ρ (same ρ as plants, K1) | Supplier utilisation | Reuses plant ρ, no separate rationale | Supplier utilisation | |
| K8 | DC/retailer capacity (`chain.py:448`) | Infinite storage and handling | Warehouses never fill up | Simplification | Structural: warehouse capacity constraints | |

---

## 9. Disruptions and outages (`generator/chain.py`)

| ID | Parameter (location) | Current value / distribution | Plain meaning | Why (honest) | Compare against | Verified value / source / confidence |
|---|---|---|---|---|---|---|
| O1 | P(config has no outages) `OUTAGE[0]` (`chain.py:105`) | 0.4 | 40% of networks never have random outages | **Uncalibrated prior.** Rough guess | Share of firms reporting a significant supply disruption per year (BCI Supply Chain Resilience Report, Resilinc EventWatch, Everstream) | |
| O2 | P(node exposed) `OUTAGE[1]` (`chain.py:105`) | U(0.2, 0.7) per config | Share of suppliers, plants and upstream DCs that can fail | **Uncalibrated prior.** Rough guess | Share of sites that suffer outages | |
| O3 | Mean time between failures `OUTAGE[2]` (`chain.py:105`) | Config mean LU(180, 1500) days; per node × LN(0, 0.5) | An exposed node fails on average every 0.5-4 years | **Uncalibrated prior.** Rough guess | Frequency of supplier disruptions (BCI: ~% of organisations with ≥1 disruption/year), plant unplanned shutdown frequency, FDA warning letters / import alerts for pharma plants | |
| O4 | Mean time to repair `OUTAGE[3]` (`chain.py:105`) | Config mean LU(2, 30) days; per node × LN(0, 0.5) | Outages last days to weeks | **Uncalibrated prior.** Rough guess | Disruption durations: FDA drug shortage durations (often many months), semiconductor fab outages (fires, earthquakes), port closures, Resilinc recovery times | |
| O5 | Outage timing shape (`chain.py:391-397`) | Exponential gaps; lognormal durations with CV 1 | Memoryless failures; durations right-skewed | Common modelling choice; CV 1 is arbitrary | Disruption duration distributions (heavy tail?) | |
| O6 | What fails (`chain.py:340-344`) | Suppliers, plants and DCs **without** customers; retailers and customer-facing DCs never fail | Outage = node stops taking orders (SupplyNetPy semantics); in-transit shipments still arrive | Simplification | Structural: real outages also hit retail and transport links, and partial capacity loss is common | |
| O7 | No transport-link disruptions | Links never fail; only lead-time noise | | Simplification | Port/route disruptions (Red Sea, Suez 2021) | |

What-if variants (methodology, §10) add one forced outage of 14-90 days at one node.

---

## 10. Simulation design (METHODOLOGY: mostly not real-world checkable)

Listed for completeness. Please only comment if something looks methodologically wrong.

| ID | Parameter (location) | Current value | Meaning | Why |
|---|---|---|---|---|
| M1 | Horizon (`dataset.py:682`, `run_build.sh`) | 1,825 days (5 years) | Simulated length per run | Pilot: 4 measured years bring median profit 95% CI under 1% of revenue |
| M2 | Warm-up (`dataset.py:683`) | 365 days, discarded | Removes start-up transient (initial stock S is free) | Pilot: transient is year 1 only (MSER-5 profit cut p50 70 d, 88% ≤ 365 d) |
| M3 | Measurement window | (365, 1825], whole years | Removes seasonal bias of the window mean | Pilot |
| M4 | Trend pivot (`chain.py:104`, `dataset.py:452`) | Day 1,095 (middle of main window) | Day where trend factor = 1, so base sizing matches mid-window demand | Design |
| M5 | Replications (`run_build.sh`) | 1 per config; 5 for 10% of base configs | Run-to-run noise estimate | Compute budget |
| M6 | What-if variants (`run_build.sh`, `dataset.py:165-182`) | 2 variants for 25% of base configs; type chosen 1/3 each: outage 14-90 d at one supplier/plant/upstream DC; plant capacity × U(0.3, 0.9); service z ± U(0.5, 1.5) | Intervention pairs with identical customer orders | Design; the outage length 14-90 d and capacity cut 0.3-0.9 are rough guesses that could be checked like O4 |
| M7 | Splits (`dataset.py:185-188`) | 70/15/15 train/val/test by graph hash | ML splits | Convention |
| M8 | Sizes in build (`run_build.sh`) | 2,200 size **draws** (`--tuples`, log-uniform, repeats allowed, so fewer distinct sizes) × up to 50 graphs × 4 scenarios | Dataset size | Compute budget |
| M9 | MSER-5 warm-up check (`chain.py:607-618`, `dataset.py:201-208`) | Weekly series, batch 5, first-half search | Per-run warm-up diagnostic | White (1997) |
| M10 | Run timeout (`dataset.py:690`) | 3,600 s | Failed-run guard | Engineering |
| M11 | Seed (`dataset.py:695`) | 20261005 | Reproducibility | Arbitrary |
| M12 | Lead-time Monte Carlo draws (`chain.py:295`) | 512 | Estimate of E[max of lead times] at plants | Numerical |
| M13 | Arrival thinning window (`chain.py:136-139`) | 30 days | Numerical method, exact | Numerical |
| M14 | WL hash iterations (`topology.py:40`) | 6 | Duplicate-graph detection | Numerical |

---

## 11. Resulting behaviour to sanity-check against reality (outputs, not inputs)

These come out of the simulation and are not set directly. If they look unrealistic, please say
**which input you think causes it**.

**Source:** the 400-run check build of code version **0.7.0** (`timing5`, 17 Oct 2026): 100 network
sizes × 4 scenarios × 1 replication, horizon 1,825 d, warm-up 365 d, seed 7, 10% large band, 308
echelon + 92 random-tree runs, 0 failures; **325 stress + 75 control** configs (§0). Same graphs and
seeds as the 0.6.0 build `timing4`. "p10 / p50 / p90" are across runs. Counts are given next to
percentages.

**0.6.0 baseline, corrected (A8):** in `timing4`, fill < 0.7 was **70/400 = 17.5%** (16/25/11/18 per
100 FMCG/electronics/pharma/industrial runs), fill < 0.5 **2/400 = 0.5%** (1/1/0/0), loss-making
**27/400 = 6.75%** (2/17/0/8). Earlier versions of this file rounded these to 17%, 1% and 7%.

### 11.0 Metric definitions

- **Fill** = customer units served at once ÷ units demanded. Unmet demand is lost, never backordered.
  This is a unit fill with lost sales, not APQC's order fill (orders filled completely) or a
  within-24-hours unit fill.
- **Margin** = (customer revenue − raw purchases − manufacturing − carrying cost − transport) ÷
  customer revenue: a **contribution after carrying cost**, not GAAP gross margin and not net margin
  (no overheads, C7). Raw purchases are booked at receipt, not as cost of goods sold, but the change in
  stock value over the window is ≤ ~2% of revenue (0.6.0 audit), so purchases ≈ COGS.
- **Gross margin** (below) = (revenue − raw − manufacturing) ÷ revenue.
- **Carrying cost** is at the consolidated cost basis (C4); **transport** = fixed freight per order +
  freight share × value received (L3, L4).
- **Stockout share** = share of days a customer-facing site **ends** with nothing on hand: an
  end-of-day snapshot, not time-weighted or arrival-weighted availability.
- **Inventory turns** = (raw + manufacturing cost per day × 365) ÷ average inventory valued at cost
  at plants, DCs and retailers (whole chain, not one echelon).
- **Days of cover** (site) = average units on hand ÷ the site's base daily flow.

### 11.1 Network-level outcomes (main window), all 400 runs

| Outcome | All | FMCG | Electronics | Pharma | Industrial | Compare against | Checker comment |
|---|---|---|---|---|---|---|---|
| Customer fill rate, p10 / p50 / p90 | 0.66 / **0.861** / 0.98 | 0.65 / 0.88 / 0.98 | 0.63 / 0.81 / 0.97 | 0.71 / 0.90 / 0.99 | 0.67 / 0.84 / 0.95 | APQC median order fill 94% (§0; different metric, see 11.0); on-shelf availability (ECR OOS ~8%) | |
| Runs with fill < 0.7 | **56/400 = 14.0%** | 12 (12%) | 21 (21%) | 10 (10%) | 13 (13%) | How common are chains with > 30% lost demand? | |
| Runs with fill < 0.5 | 3/400 = 0.75% | 1 | 2 | 0 | 0 | | |
| Margin, p10 / p50 / p90 | 0.06 / 0.36 / 0.62 | 0.06 / 0.38 / 0.51 | −0.01 / 0.24 / 0.38 | 0.20 / 0.48 / 0.68 | 0.09 / 0.40 / 0.64 | End-to-end contribution margin by industry | |
| Loss-making runs | **21/400 = 5.25%** | 3 | **13** | 0 | 5 | Share of loss-making product lines | |
| Gross margin, p50 | 0.42 | 0.43 | 0.31 | 0.53 | 0.49 | Manufacturer + retailer gross margin (Best Buy 22.5%, Fastenal 45.0%: single companies) | |
| Inventory turns at cost, p10 / p50 / p90 | 2.6 / 7.6 / 28.7 | 9.5 / 19.8 / 49.1 | 2.7 / 7.5 / 16.6 | 2.6 / 6.4 / 20.9 | 2.0 / 4.7 / 9.6 | Best Buy 6.3, Fastenal 2.7, DMart 11.6 (§0; company networks) | |
| Holding cost / revenue, p50 | 1.4% | 0.6% | 2.4% | 1.4% | 2.6% | Carrying cost as % of sales. 0.6.0: 2.3% (transfer prices, 0.20+ rates) | |
| Transport cost / revenue, p50 (p10-p90) | **3.75%** (1.7-7.6%) | 4.0% | 4.3% | 2.4% | 5.2% | Transport cost % of sales by industry; India logistics 7.97% of GDP is a comparator only | |
| Stockout share (customer-facing sites), p50 | 7.2% | 6.8% | 11.8% | 5.5% | 6.1% | Out-of-stock frequency | |

By topology: echelon 39/308 fill < 0.7, 0 below 0.5, 14 loss-making, fill p50 0.863; random tree
17/92, 3, 7, fill p50 0.847. Large band (n 150-300): 10/72, 0, 3, fill p50 0.871.

### 11.2 Stress vs control

| Group | Runs | Fill < 0.7 | Fill < 0.5 | Loss | Fill p50 | Margin p50 | Holding / rev. p50 | Transport / rev. p50 |
|---|---|---|---|---|---|---|---|---|
| **Stress, all** | 325 | 56 (17.2%) | 3 (0.9%) | 21 (6.5%) | 0.83 | 0.32 | 1.35% | 3.9% |
| FMCG | 80 | 12 (15.0%) | 1 | 3 (3.8%) | 0.85 | 0.32 | 0.60% | 4.1% |
| Electronics | 85 | 21 (24.7%) | 2 | 13 (15.3%) | 0.79 | 0.20 | 2.3% | 4.4% |
| Pharma | 80 | 10 (12.5%) | 0 | 0 | 0.87 | 0.44 | 1.2% | 2.5% |
| Industrial | 80 | 13 (16.2%) | 0 | 5 (6.2%) | 0.81 | 0.36 | 2.4% | 5.3% |
| **Control, all** | 75 | 0 | 0 | 0 | 0.97 | 0.49 | 1.6% | 3.1% |
| FMCG | 20 | 0 | 0 | 0 | 0.97 | 0.46 | 0.50% | 3.0% |
| Electronics | 15 | 0 | 0 | 0 | 0.97 | 0.37 | 3.8% | 3.8% |
| Pharma | 20 | 0 | 0 | 0 | 0.99 | 0.64 | 1.5% | 2.0% |
| Industrial | 20 | 0 | 0 | 0 | 0.94 | 0.55 | 3.3% | 4.9% |

The stress regime was not re-tuned: its failure rates stay near the 0.6.0 targets (17.2% vs 17.5%
fill < 0.7), and the overall rates fall because of the 20% control share. Control fill p10 is 0.91.

### 11.3 Site-level days of cover (average on-hand ÷ base daily flow), p10 / p50 / p90

| Stage | FMCG | Electronics | Pharma | Industrial | Compare against | Checker comment |
|---|---|---|---|---|---|---|
| Plant, all | 2.2 / 7.1 / 16 | 7.0 / 19 / 52 | 8.8 / 29 / 79 | 12 / 30 / 64 | Manufacturer finished-goods days of supply | |
| DC without customers, all | 2.8 / 7.0 / 16 | 7.9 / 19 / 39 | 9.3 / 21 / 43 | 14 / 34 / 74 | DC days of supply | |
| Customer-facing site, all | 1.3 / 2.8 / 5.5 | 3.1 / 6.5 / 14 | 3.1 / 6.5 / 12 | 8.7 / 19 / 34 | Store days of supply | |
| Plant / DC / retailer p50, stress | 6.4 / 6.2 / 2.4 | 17 / 17 / 5.8 | 22 / 17 / 5.3 | 27 / 30 / 17 | | |
| Plant / DC / retailer p50, control | 10 / 11 / 4.2 | 35 / 33 / 13 | 47 / 31 / 9.4 | 44 / 50 / 29 | | |

Site fill p10 / p50 / p90: plants 0.47 / 0.86 / 1.00; DCs 0.54 / 0.92 / 1.00; retailers 0.64 / 0.89 / 0.99.
Network days of cover (product units on hand ÷ customer demand per day, whole chain) p10 / p50 / p90
= 9.9 / 39 / 124; FMCG 6 / 16 / 35, electronics 15 / 38 / 124, pharma 14 / 53 / 125, industrial 29 / 66 / 146.

### 11.4 New 0.7.0 diagnostics

| Diagnostic | Value | Meaning |
|---|---|---|
| Actual / planned lead time, DC and retailer (`lead_time_obs` ÷ `lead_time`), p50 / p90 | all 1.08 / 1.78; stress 1.11 / 1.90; control 1.02 / 1.19 | Waiting at a short supplier is not in the plan (S12). Excess p50 0.3 d, p90 3.2 d. By scenario (all) p50 / p90: FMCG 1.06 / 1.74, electronics 1.10 / 1.73, pharma 1.08 / 1.82, industrial 1.08 / 1.88 |
| Same, p90 order ÷ planned L, median site | 1.46 (stress 1.51, control 1.32) | One order in ten takes this much longer than planned at the median site |
| Plant raw orders (`lead_time_obs` ÷ mean inbound `lt_mean`), p50 / p90 | 1.00 / 1.04 | Infinite or floor-sized finite suppliers almost never make a plant wait |
| Plants with `peak_load` > 1 | **61%** (755 of 1,236); stress 62%, control 58%; FMCG 70%, electronics 61%, pharma 56%, industrial 57%; p50 / p90 1.06 / 1.40 | Capacity is sized at pivot-day demand (K1, deliberate stress). Up from ~51% in the 0.6.0 audit because seasonality no longer cancels across streams (D7) |
| Network seasonal amplitude ÷ config amplitude, p10 / p50 / p90 (280 seasonal configs) | **0.75 / 0.93 / 1.09** (0.6.0: 0.16 / 0.53 / 1.01); share below 0.5: 0% (0.6.0: 46%) | Effect of the phase-spread cut to U(0, π/4) |
| Per-run wall time, p50 / mean | 3.9 s / 8.4 s (0.6.0 `timing4`: 3.0 s / 6.4 s) | The +24% was uniform across regimes and also on configs whose simulation is unchanged, and the laptop's load average was ~8 during the build; a CPU-time A/B of the new order-timing code showed ≤ ~5%, within noise. Sizing is kept at 0.0712·n − 0.13 s |

### 11.5 Network shape outcomes

| Outcome | Value | Compare against | Checker comment |
|---|---|---|---|
| Depth (longest path supplier → customer, edges), echelon | p10 3 / p50 4 / p90 5 | Number of echelons in real distribution networks | |
| Depth, random tree | p10 6 / p50 11 / p90 17 (deliberately unrealistic stratum) | | |
| DC tiers drawn (echelon, 77 graphs) | 0: 29, 1: 36, 2: 12, 3: 0 (fewer deep networks than the N3 prior, because small n drops tiers) | | |
| DC : retailer ratio (pure DCs ÷ customer-facing sites) | 0.246 overall, **0.162 echelon only** (~6 retailers per DC), 0.767 random trees | DMart 5.5 stores per DC, Walmart US 27 per facility (§0) | |

---

## 12. Known simplifications and limitations

Please flag any of these that make the data unrealistic in a way the numbers can't fix.

1. **Single product.** No product mix, no shared capacity between products, no substitution.
2. **BOM of 1-3 inputs**, each from one supplier, integer quantities. Real BOMs (especially
   electronics) have hundreds of parts.
3. **No price elasticity or promotions.** Prices are fixed, and demand does not respond to price,
   stockouts elsewhere or promotions.
4. **Lost sales only.** Customers never wait or backorder, and there is no substitution.
   Site-to-site orders do wait (backorder) until stock is available.
5. **Demand is independent across customer streams** apart from a shared config trend and season.
   There are no common shocks (e.g. COVID, festivals) and no weekly or day-of-week pattern.
6. **Seasonality is one smooth annual sinusoid.** Trend and season are **not anticipated** by planners.
7. **Retailers and customer-facing DCs are never disrupted.** Transport links never fail. An outage
   only stops a node taking new orders; shipments already in transit still arrive. Outages are full
   on/off, never partial capacity loss.
8. **Infinite storage and handling capacity** at DCs and retailers. Plant capacity is one batch at a
   time; there are no shifts, changeovers or multiple lines.
9. **Freight is proportional to the value received**, not weight, volume or distance, plus a fixed
   per-order part that is derived from the EOQ assumption rather than measured.
10. **No overheads.** Margin is a contribution margin (no SG&A, labour, rent, R&D, tax), so it should
    be compared with gross or contribution margins, not net margins.
11. **No expiry, obsolescence events or returns.** Obsolescence only enters as a higher holding rate.
    Pharma expiry and cold chain are not modelled.
12. **Static policies.** s, S and review periods are fixed for 5 years, with no re-planning,
    expediting or emergency orders. Suppliers are not switched during an outage, except that
    multi-sourced sites skip a disrupted parent.
13. **Planners use the same structure everywhere:** (s,S) with normal-approximation safety stock.
    Real firms use MRP/DRP, min-max, VMI and so on.
14. **Each industry's money scale is arbitrary**; only ratios are meaningful.
15. **Network size is capped at 300 nodes**, and demand per retailer does not depend on network size.
    Bigger networks are therefore just more retailers, not bigger retailers.
16. **No India-specific structure** such as C&F agents, super-stockists or kirana credit cycles,
    except as generic DC tiers.

### 12.1 Possible future work (deferred, not in 0.7.0)

The review proposed these; each changes behaviour and needs a decision or new evidence first.

- **Long electronics procurement:** a case with supplier lead times of ~8-20 weeks (component shortages)
  for some electronics configs, instead of extra transit.
- **Pharma QA release delay:** a fixed batch-release time (often weeks) added to the plant→DC lead time.
- **Long-outage tail:** for some outage configs, mean repair times of 60-120 days (staged recovery, cf.
  Renesas 97 days).
- **k = 3 DC tiers:** the N3 prior gives 10% three-tier networks, but the size shrink never realises them.
- **Partial outages:** capacity loss instead of a full stop in order intake; needs a custom process.

---

## 13. What we would like back (output format)

Please return one entry per parameter ID (N1 … I23, D1 …, S1 …, L1 …, C1 …, K1 …, O1 …), in this format:

```
### <ID> <parameter name>
- Our current value: <copy from above>
- Proposed value or range: <value / range and distribution if relevant>, or "keep", or "cannot be pinned down"
- Evidence: <1-3 sentences summarising what the sources say>
- Sources: <title, publisher, year, URL> (one per line; prefer primary data: 10-Ks, government
  statistics, industry associations, peer-reviewed studies; say if a source is a blog or vendor)
- Confidence: high / medium / low
- India vs global: <different? how? or "not relevant">
- Structural issue (optional): <if the parameter is the wrong concept or wrong distribution,
  say what it should be instead>
```

Also please give:
1. **Outcome check (§11):** for each outcome row, say whether our value falls inside the real-world
   range. If it does not, say which input(s) you would change.
2. **Structural flags:** any parameter or simplification (§12) you think is structurally wrong, not just
   mis-valued, ranked by how much it hurts realism.
3. **Missing parameters:** anything a realistic supply-chain simulation should vary that we don't vary.
4. **Unknowns:** parameters where you found no usable evidence. Please say so explicitly rather than
   guessing.

Where real data differs sharply between sub-segments (e.g. generic vs branded pharma, kirana vs
modern trade, smartphones vs appliances), please say so and suggest whether we should split the
scenario or widen the range.

Thank you. We expect some of our numbers are wrong, so please correct whatever needs correcting.
