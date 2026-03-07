use ethers::types::Address;
use eyre::Result;
use serde::{Deserialize, Serialize};

// Odos V2 API — Arbitrum (chain ID 42161)
// Docs: https://docs.odos.xyz
const ODOS_QUOTE_URL: &str = "https://api.odos.xyz/sor/quote/v2";
const ARBITRUM_CHAIN_ID: u64 = 42161;

// Placeholder executor address for quote requests (no real tx needed for quotes)
const QUOTE_USER_ADDR: &str = "0x0000000000000000000000000000000000000001";

// ─────────────────────────────────────────────
//  ODOS API REQUEST / RESPONSE STRUCTS
// ─────────────────────────────────────────────

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct OdosInputToken {
    token_address: String,
    amount: String, // in wei, as string
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct OdosOutputToken {
    token_address: String,
    proportion: f64, // 1.0 = 100% goes to this token
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct OdosQuoteRequest {
    chain_id: u64,
    input_tokens: Vec<OdosInputToken>,
    output_tokens: Vec<OdosOutputToken>,
    user_addr: String,
    slippage_limit_percent: f64,
    compact: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct OdosQuoteResponse {
    out_amounts: Vec<String>,  // Output token amounts in wei
    gas_estimate: Option<f64>, // Estimated gas units
    path_id: Option<String>,   // Used for assembling the actual swap tx in M24
}

// ─────────────────────────────────────────────
//  PUBLIC OPPORTUNITY STRUCT
// ─────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct LiquidationOpportunity {
    pub target_user:         Address,
    pub collateral_asset:    Address,
    pub debt_asset:          Address,
    pub debt_to_cover:       u128,       // Wei — 50% of total debt (Aave close factor)
    pub collateral_to_seize: u128,       // Wei — collateral we receive + 5% bonus
    pub expected_revenue:    f64,        // USD — what Odos says we'll get
    pub flashloan_cost:      f64,        // USD — debt we must repay
    pub estimated_gas_usd:   f64,        // USD — gas cost
    pub net_profit_usd:      f64,        // USD — our take-home profit
    pub health_factor:       f64,        // HF that triggered this (< 1.0)
    pub odos_path_id:        Option<String>, // Saved for M24 tx assembly
}

// ─────────────────────────────────────────────
//  MAIN: CALCULATE PROFIT VIA ODOS
// ─────────────────────────────────────────────
pub async fn calculate_profit(
    target_user: Address,
    collateral_asset: Address,
    debt_asset: Address,
    collateral_amount_wei: u128,
    total_debt_wei: u128,
    health_factor: f64,
    http_client: &reqwest::Client,
    odos_api_key: &str,
) -> Result<Option<LiquidationOpportunity>> {
    // Aave close factor: we can repay 50% of debt per call
    let debt_to_cover = total_debt_wei / 2;

    // Seized collateral = 50% of their collateral + 5% liquidation bonus
    let collateral_to_seize = (collateral_amount_wei as f64 * 0.5 * 1.05) as u128;

    // Build the Odos quote request
    let request_body = OdosQuoteRequest {
        chain_id: ARBITRUM_CHAIN_ID,
        input_tokens: vec![OdosInputToken {
            token_address: format!("{:#x}", collateral_asset),
            amount: collateral_to_seize.to_string(),
        }],
        output_tokens: vec![OdosOutputToken {
            token_address: format!("{:#x}", debt_asset),
            proportion: 1.0,
        }],
        user_addr: QUOTE_USER_ADDR.to_string(),
        slippage_limit_percent: 0.5,
        compact: true,
    };

    // Send request to Odos
    let mut req = http_client.post(ODOS_QUOTE_URL).json(&request_body);

    // Add API key header if provided
    if !odos_api_key.is_empty() {
        req = req.header("Authorization", format!("Bearer {}", odos_api_key));
    }

    let response = req.send().await;

    let quote: OdosQuoteResponse = match response {
        Ok(resp) if resp.status().is_success() => {
            match resp.json::<OdosQuoteResponse>().await {
                Ok(q) => q,
                Err(e) => {
                    eprintln!("  ⚠️  Odos JSON parse error for {:?}: {}", target_user, e);
                    return Ok(None);
                }
            }
        }
        Ok(resp) => {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            eprintln!("  ⚠️  Odos API error {} for {:?}: {}", status, target_user, body);
            return Ok(None);
        }
        Err(e) => {
            eprintln!("  ⚠️  Odos request failed for {:?}: {}", target_user, e);
            return Ok(None);
        }
    };

    // Parse the output amount (debt token, 6 decimals for USDC/USDT)
    let revenue_raw: u128 = quote.out_amounts.first()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let expected_revenue = revenue_raw as f64 / 1_000_000.0; // USDC/USDT = 6 decimals

    // Cost: what we must repay (debt_to_cover + 0.05% Aave V3 Flashloan fee)
    let flashloan_fee = debt_to_cover as f64 * 0.0005; 
    let flashloan_cost = (debt_to_cover as f64 + flashloan_fee) / 1_000_000.0; // 6 decimals


    // Gas cost estimate: Arbitrum is very cheap
    // ~800k gas at 0.1 gwei × $3,000/ETH ≈ $0.24
    let gas_units = quote.gas_estimate.unwrap_or(800_000.0);
    let estimated_gas_usd = (gas_units * 0.1e-9) * 3_000.0;

    let net_profit_usd = expected_revenue - flashloan_cost - estimated_gas_usd;

    Ok(Some(LiquidationOpportunity {
        target_user,
        collateral_asset,
        debt_asset,
        debt_to_cover,
        collateral_to_seize,
        expected_revenue,
        flashloan_cost,
        estimated_gas_usd,
        net_profit_usd,
        health_factor,
        odos_path_id: quote.path_id,
    }))
}

// ─────────────────────────────────────────────
//  SORT & FILTER
// ─────────────────────────────────────────────
pub fn sort_and_filter(
    mut opportunities: Vec<LiquidationOpportunity>,
    min_profit_usd: f64,
) -> Vec<LiquidationOpportunity> {
    opportunities.retain(|op| op.net_profit_usd >= min_profit_usd);
    opportunities.sort_by(|a, b| {
        b.net_profit_usd.partial_cmp(&a.net_profit_usd).unwrap()
    });
    opportunities
}
