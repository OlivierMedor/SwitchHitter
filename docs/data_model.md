# Switch-Hitter Database Schema

## Overview
The Postgres database serves as the single source of truth for the Switch-Hitter MEV framework. All collected, raw, and enriched data, as well as the queue state for the microservices, lives here.

## Tables

### `liquidations`
Stores the historical `LiquidationCall` events emitted by the Aave v3 Pool contract.

| Column Name | Data Type | Description |
| :--- | :--- | :--- |
| `id` | `SERIAL` | Primary key. |
| `tx_hash` | `VARCHAR(66)` | Unique transaction hash of the liquidation. |
| `block_number` | `BIGINT` | Block number where the transaction was mined. |
| `timestamp` | `TIMESTAMP` | Block timestamp. |
| `collateral_asset` | `VARCHAR(42)` | Address of the collateral asset. |
| `debt_asset` | `VARCHAR(42)` | Address of the debt asset. |
| `user_address` | `VARCHAR(42)` | Address of the liquidated user. |
| `liquidator_address` | `VARCHAR(42)` | Address of the liquidator. |
| `debt_to_cover` | `NUMERIC` | Amount of debt covered (raw precision). |
| `liquidated_collateral_amount` | `NUMERIC` | Amount of collateral liquidated (raw precision). |
| `status` | `VARCHAR(20)` | Queue status: `'raw'` (from Collector) or `'enriched'` (from Enricher). |
| `gas_used_units` | `BIGINT` | [Enriched] Raw gas units used by the transaction. |
| `gas_cost_eth` | `DECIMAL` | [Enriched] Total gas cost in ETH (units * effective gas price). |
| `competitor_attempts` | `INTEGER` | [Enriched] Number of failed/reverted transaction attempts targeting the same underwater position in the same block. |
| `created_at` | `TIMESTAMP` | Record creation. |
| `updated_at` | `TIMESTAMP` | Record last updated. |

## Workflow States
1. **Collector** inserts a new row with `status = 'raw'`.
2. **Enricher** queries for rows where `status = 'raw'`, performs on-chain block parsing to find gas usage (`gas_used_units`, `gas_cost_eth`) and `competitor_attempts`, updates these fields, and sets `status = 'enriched'`.
3. **Dashboard / Backtester** query only rows where `status = 'enriched'` for analysis.
