# Glossary

Terms used in these pages and in the column names, in alphabetical order.

| Term | Meaning |
|---|---|
| **Base setup** | A setup without a what-if change. Its `base_config_id` equals its `config_id`. |
| **Batch** | The amount a factory makes in one go. One batch at a time, so the batch size sets the factory's capacity. |
| **Breakdown (outage)** | A period when a supplier, factory or warehouse takes no new orders. |
| **Config** | The code's word for a setup. |
| **Control setup** | A well-run setup: honest forecasts, normal prices, a 90%–98% service target. 20% of setups. |
| **Customer stream (D)** | The flow of customer orders at one shop. |
| **DC (distribution centre)** | A warehouse between factories and shops. |
| **Delivery time (lead time)** | Days from placing an order to receiving it. |
| **Echelon / layer** | One level of the chain: suppliers, factories, a warehouse layer, shops. A *layered* (echelon) network has goods flowing level by level. |
| **EOQ** | Economic order quantity: the order size that balances the cost of ordering against the cost of holding stock. |
| **Fill rate** | The share of demand served at once from the shelf. 1.0 means every unit was served. |
| **Flow** | The average number of units a site passes on per day. |
| **FMCG** | Fast-moving consumer goods, such as groceries and toiletries. |
| **Forecast bias** | How far the planner's demand estimate is from the truth. Negative means too low. |
| **Graph** | The code's word for a network. |
| **Holding cost** | The cost of keeping stock: money tied up, storage, insurance, spoilage. Given as a share of the stock's value per year. |
| **Lost sale** | Demand that could not be served from the shelf and is gone. Customers here never wait. |
| **Margin** | Profit divided by sales. |
| **MSER-5** | An automatic test for the length of the warm-up period of a simulation. |
| **Network** | The map of who supplies whom. |
| **Node** | One point in the network: a supplier, factory, warehouse, shop or customer stream. |
| **Parquet** | The file format of the tables: compressed, by column, readable from most data tools. |
| **Peak load** | How busy a factory is at the seasonal peak, compared with its capacity. Above 1 means it cannot keep up. |
| **Recipe** | Units of each raw material needed for one unit of product (1–3). |
| **Regime** | `stress` or `control`. |
| **Replication (repeat)** | Another run of the same setup with different random luck. |
| **Review period (r)** | How often a site checks its stock: every day or every week. |
| **Run** | One 5-year simulation of a setup. |
| **(s, S) rule** | The stock rule: when the stock position falls to s or below, order up to S. |
| **Safety factor (z)** | How much extra stock is kept for surprises, in units of demand uncertainty. Higher z, fewer shortages. |
| **Scenario** | The code's word for industry: `fmcg`, `electronics`, `pharma`, `industrial`. |
| **Seed** | The starting number for all randomness. Same seed, same dataset. |
| **Series** | The day-by-day history of a run. |
| **Service target** | The chance of not running out during one reorder cycle that a site aims for. It sets z. |
| **Setup** | One network with all its numbers filled in, for one industry. |
| **Shop (retailer)** | A site that sells to customers. |
| **Skip link** | A link that jumps over a layer, for example factory straight to shop. |
| **Split** | The machine-learning group of a network: `train`, `val` or `test`. |
| **Stock position** | Stock on the shelf + stock on its way − stock still owed to others. |
| **Stress setup** | A setup that may be badly run on purpose (wrong forecasts, price wars, low stock). 80% of setups. |
| **Supplier, finite / infinite** | A finite supplier produces at a fixed rate into a limited store and can run out. An infinite one never runs out. |
| **Warm-up** | The first simulated year. It is left out of the results because the full starting shelves make it unrealistic. |
| **What-if copy (variant)** | A copy of a base setup with one change (breakdown, smaller factory, different safety stock) and the same random luck. |
