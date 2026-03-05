CREATE TABLE IF NOT EXISTS liquidations (
    id SERIAL PRIMARY KEY,
    tx_hash VARCHAR(66) NOT NULL,
    log_index BIGINT NOT NULL,
    block_number BIGINT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    collateral_asset VARCHAR(42) NOT NULL,
    debt_asset VARCHAR(42) NOT NULL,
    user_address VARCHAR(42) NOT NULL,
    liquidator_address VARCHAR(42) NOT NULL,
    debt_to_cover NUMERIC NOT NULL,
    liquidated_collateral_amount NUMERIC NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'raw',
    gas_used_units BIGINT,
    gas_cost_eth DECIMAL,
    competitor_attempts INTEGER,
    net_profit_usd DECIMAL,
    quoted_slippage_bps DECIMAL,
    quoted_swap_output VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tx_hash, log_index)
);

-- Index for querying raw liquidations effectively in the Enricher
CREATE INDEX idx_liquidations_status ON liquidations(status);

-- Index for faster block and time based queries
CREATE INDEX idx_liquidations_block_number ON liquidations(block_number);
CREATE INDEX idx_liquidations_timestamp ON liquidations(timestamp);

-- --- V2 EXECUTION LOG ---
-- Tracks every liquidation attempt made by our Rust bot in production
CREATE TABLE IF NOT EXISTS bot_executions (
    id SERIAL PRIMARY KEY,
    attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    target_user VARCHAR(42) NOT NULL,
    protocol VARCHAR(20) NOT NULL DEFAULT 'aave_v3',
    debt_asset VARCHAR(42) NOT NULL,
    collateral_asset VARCHAR(42) NOT NULL,
    flashloan_provider VARCHAR(20) NOT NULL,  -- 'balancer' or 'aave'
    health_factor_at_trigger DECIMAL,
    expected_profit_usd DECIMAL,
    actual_profit_usd DECIMAL,
    gas_cost_eth DECIMAL,
    tx_hash VARCHAR(66),
    status VARCHAR(20) NOT NULL DEFAULT 'pending', -- pending, success, failed, beaten
    wallet_used VARCHAR(42),
    error_message TEXT
);

CREATE INDEX idx_bot_executions_attempted_at ON bot_executions(attempted_at);
CREATE INDEX idx_bot_executions_status ON bot_executions(status);

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
