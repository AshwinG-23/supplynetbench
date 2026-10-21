# Assumptions and limits

Every simulation simplifies the real world. This page lists what is simplified, why, and what it means
for anyone using the data. A longer list, with every number and its status, is kept in `check.md` in the
repository.

## What kind of data this is

The dataset is a **scenario generator, partly anchored to public evidence**. It is **not** fitted to any
real company or industry:

- A handful of numbers are compared with dated public figures (table below).
- Every other number is a **reasoned design choice**: a range we judge plausible, kept wide on purpose
  for variety, and open to change when better evidence arrives.

The results should be read as *"what this kind of network tends to do under these rules"*, not as a
forecast for a real business.

## Main assumptions

| # | Assumption | Why | What it means for users |
|---|---|---|---|
| 1 | **One product.** Every factory makes the same product | Keeps each network readable and matches SupplyNetPy's one-product-per-site model | Results say nothing about product mixes or shared capacity between products |
| 2 | **Lost sales.** Customers take what is on the shelf; the rest is lost | Common in retail; avoids an edge case in customer waiting | Fill rate is "served at once"; there are no customer backorders |
| 3 | **Sites wait for supplies.** Orders between sites wait until they can be shipped in full | SupplyNetPy behaviour | A short supplier delays its children instead of cancelling their orders |
| 4 | **Planners plan for normal days.** Stock rules ignore season, growth and breakdowns | Many real plans do the same; it creates realistic stress | Seasonal peaks and long breakdowns cause shortages by design |
| 5 | **Factories are sized for normal demand** (60%–97% busy) | Deliberate stress | 61% of factories cannot keep up at their seasonal peak (`peak_load` > 1) |
| 6 | **Unlimited storage space** at factories, warehouses and shops | Keeps the focus on flow, not floor space | Stock is limited only by the stock rule |
| 7 | **Breakdowns stop new orders**, but goods already on the road still arrive | SupplyNetPy behaviour | A breakdown hurts after a delay, as stock runs down |
| 8 | **Whole-number recipes** (1–3 units of each raw material) | Avoids a rounding edge case in the library | — |
| 9 | **Fixed supplier weights** for sites with two suppliers | Simple, common practice | No smart switching between suppliers, except skipping one that is broken down |
| 10 | **Money without currency.** Each setup has its own price units | Industries differ by orders of magnitude | Compare ratios (margin, cost shares), not absolute money across setups |
| 11 | **Stress setups are badly run on purpose** (80%) | So the data contains failures | Use the `regime` column to separate them from the well-run 20% |

## Checked against public figures

Each of these is a **comparison**, not a fit: the public figure and our number do not always measure
exactly the same thing.

| Topic | Public figure | Our number |
|---|---|---|
| Holding cost | APQC median: 10% of inventory value per year, 6,468 organisations [^apqc] | 10%–40% a year (lower end matches the median) |
| Fill rate | APQC median order fill rate: 94% [^apqcfill] | Control setups: median fill 0.94–0.98. APQC counts whole orders, we count units, so this is only a rough comparison |
| Factory use | RBI: Indian manufacturing ran at 74.6% of capacity, Q1 2026-27 [^rbi] | Factories busy 60%–97% on a normal day |
| Medicine margins | NPPA ceiling price = price to retailer × 1.16, i.e. a 13.8% margin [^nppa] | Used to sense-check pharma shop margins |
| Stores per warehouse | DMart: 415 stores, 75 DCs, about 5.5 stores per DC [^dmart] | About 6 shops per warehouse in layered networks |
| Long breakdowns | Renesas Naka fire: about 97 days to full production [^renesas] | Breakdowns last days to weeks on average, with a rare long tail |
| Logistics cost | India: 7.97% of GDP, 2023-24 [^dpiit] | Freight 0.3%–3% of goods value per leg (different measure; comparison only) |

## Known limits

- **Three warehouse layers are rare** (about 1% of layered networks), below the 10% target.
- **Large networks are few**: about 5% of sizes are above 150 nodes.
- **Rare long zero-sales spells.** A small number of runs sell nothing for several months, after a very
  long breakdown of a factory's only supplier. This is allowed by the breakdown model, not a fault, but
  users who want "normal" runs may filter on `main_outage_days`.
- **Not modelled**: several products, customer waiting, transport capacity, partial breakdowns (a site
  working at half speed), long electronics purchasing times, and quality-control delays in medicines.
  These are candidates for later versions.

## Fixed in this version

Version 0.7.1 fixes a stall found in the demo build: after a breakdown, a factory could place one
raw-material order larger than its limited supplier's whole store. SupplyNetPy ships orders whole, so
that order could never be filled and the network stopped selling. Limited suppliers' stores are now
always at least as large as the largest order a factory can place
([How the numbers are chosen](numbers.md#7-limited-suppliers)).

[^apqc]: APQC, measure 100784. <https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/inventory-carrying-cost-percentage>
[^apqcfill]: APQC, "Order fill rate", measure 101445. <https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/order-fill-rate>
[^rbi]: Reserve Bank of India, OBICUS. <https://www.rbi.org.in/scripts/PublicationsView.aspx?id=24087>
[^nppa]: National Pharmaceutical Pricing Authority, proactive disclosure on ceiling-price method. <https://www.nppa.gov.in/en/proactivedisclosure>
[^dmart]: Avenue Supermarts (DMart), Annual Report FY2024-25. <https://www.msei.in/SX-Content/Listing/Annual-Reports/2025/DMART-2025.pdf>
[^renesas]: Renesas Electronics, Naka Factory fire update, 2021. <https://www.renesas.com/en/about/press-room/update-10-notice-regarding-semiconductor-manufacturing-factory-naka-factory-fire-production-capacity>
[^dpiit]: DPIIT and NCAER, *Assessment of Logistics Cost in India*, 2025. <https://www.dpiit.gov.in/static/uploads/2025/09/c0d7c33f0b737ed71728ff6c2a35f1e7.pdf>
