CREATE TABLE IF NOT EXISTS liquidations (
    id SERIAL PRIMARY KEY,
    tx_hash VARCHAR(66) UNIQUE NOT NULL,
    block_number BIGINT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    collateral_asset VARCHAR(42) NOT NULL,
    debt_asset VARCHAR(42) NOT NULL,
    user_address VARCHAR(42) NOT NULL,
    liquidator_address VARCHAR(42) NOT NULL,
    debt_to_cover NUMERIC NOT NULL,
    liquidated_collateral_amount NUMERIC NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'raw',
    gas_used NUMERIC,
    competitor_attempts INTEGER,
    net_profit_usd DECIMAL,
    quoted_slippage_bps DECIMAL,
    quoted_swap_output VARCHAR(100),
    price_block_before DECIMAL,
    price_block_after DECIMAL,
    price_block_plus_10 DECIMAL,
    price_block_plus_50 DECIMAL,
    scavenger_revenue_usd DECIMAL,
    scavenger_profit_usd DECIMAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for querying raw liquidations effectively in the Enricher
CREATE INDEX idx_liquidations_status ON liquidations(status);

-- Index for faster block and time based queries
CREATE INDEX idx_liquidations_block_number ON liquidations(block_number);
CREATE INDEX idx_liquidations_timestamp ON liquidations(timestamp);

-- Trigger to automatically update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_liquidations_updated_at
    BEFORE UPDATE ON liquidations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
