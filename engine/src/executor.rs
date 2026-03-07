use ethers::prelude::*;
use ethers::abi::{self, Token};
use ethers::types::{Address, Bytes, U256};
use ethers::types::transaction::eip2718::TypedTransaction;
use ethers::types::transaction::eip1559::Eip1559TransactionRequest;
use eyre::Result;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use crate::aggregator::LiquidationOpportunity;

// ─────────────────────────────────────────────
//  ODOS ASSEMBLE API
// ─────────────────────────────────────────────
const ODOS_ASSEMBLE_URL: &str = "https://api.odos.xyz/sor/assemble";
const ODOS_ROUTER_V2: &str = "0x19cEeAd7105607Cd444F5ad10dd51356cc098b64";

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct OdosAssembleRequest {
    user_addr: String,
    path_id: String,
    simulate: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct OdosAssembleResponse {
    transaction: OdosTransaction,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct OdosTransaction {
    data: String,
}

// ─────────────────────────────────────────────
//  EXECUTION RESULT
// ─────────────────────────────────────────────
#[derive(Debug)]
pub enum ExecutionResult {
    DryRun { calldata_preview: String, estimated_profit: f64 },
    Success { tx_hash: H256, profit_usd: f64 },
    Failed { reason: String },
}

// ─────────────────────────────────────────────
//  WALLET FLEET
// ─────────────────────────────────────────────
pub struct WalletFleet {
    pub wallets: Vec<LocalWallet>,
}

impl WalletFleet {
    pub fn load_from_env() -> Result<Self> {
        let mut wallets = Vec::new();
        let mut i = 1;
        loop {
            let key = format!("BOT_WALLET_{}", i);
            match std::env::var(&key) {
                Ok(pk) if !pk.is_empty() => {
                    let wallet: LocalWallet = pk.parse::<LocalWallet>()
                        .map_err(|e| eyre::eyre!("Invalid key in {}: {}", key, e))?
                        .with_chain_id(42161u64);
                    println!("  💼 Wallet {}: {:?}", i, wallet.address());
                    wallets.push(wallet);
                    i += 1;
                }
                _ => break,
            }
        }
        if wallets.is_empty() {
            return Err(eyre::eyre!("No wallets found! Set BOT_WALLET_1 in engine/.env"));
        }
        println!("  ✅ {} wallet(s) loaded.", wallets.len());
        Ok(Self { wallets })
    }

    pub fn pick_wallet(&self, index: usize) -> &LocalWallet {
        &self.wallets[index % self.wallets.len()]
    }
}

// ─────────────────────────────────────────────
//  ABI ENCODE: triggerLiquidation()
//
//  Function signature:
//  triggerLiquidation(address targetUser, address debtAsset,
//    address collateralAsset, uint256 debtToCover,
//    address aggregatorTarget, bytes aggregatorData)
// ─────────────────────────────────────────────
fn encode_trigger_liquidation(
    target_user: Address,
    debt_asset: Address,
    collateral_asset: Address,
    debt_to_cover: u128,
    aggregator_target: Address,
    aggregator_data: Vec<u8>,
) -> Bytes {
    // Function selector = keccak256("triggerLiquidation(address,address,address,uint256,address,bytes)")[0..4]
    let selector = &ethers::utils::keccak256(
        b"triggerLiquidation(address,address,address,uint256,address,bytes)"
    )[..4];

    let encoded_params = abi::encode(&[
        Token::Address(target_user),
        Token::Address(debt_asset),
        Token::Address(collateral_asset),
        Token::Uint(U256::from(debt_to_cover)),
        Token::Address(aggregator_target),
        Token::Bytes(aggregator_data),
    ]);

    let mut calldata = selector.to_vec();
    calldata.extend_from_slice(&encoded_params);
    Bytes::from(calldata)
}

// ─────────────────────────────────────────────
//  MAIN EXECUTOR
// ─────────────────────────────────────────────
pub async fn execute_liquidation(
    opportunity: &LiquidationOpportunity,
    wallet: &LocalWallet,
    provider: Arc<Provider<Ws>>,
    contract_address: Address,
    http_client: &reqwest::Client,
    odos_api_key: &str,
    live_mode: bool,
) -> Result<ExecutionResult> {
    let wallet_addr = wallet.address();

    println!("🔫 Preparing execution: {:?}", opportunity.target_user);
    println!("   HF: {:.4} | Est. Profit: ${:.2}", opportunity.health_factor, opportunity.net_profit_usd);

    let odos_router: Address = ODOS_ROUTER_V2.parse()?;

    // ── Step 1: Get Odos swap calldata ────────────────────────────────────────
    let aggregator_data: Vec<u8> = if let Some(path_id) = &opportunity.odos_path_id {
        println!("   📞 Assembling Odos swap calldata...");

        let body = OdosAssembleRequest {
            user_addr: format!("{:#x}", contract_address),
            path_id: path_id.clone(),
            simulate: false,
        };

        let mut req = http_client.post(ODOS_ASSEMBLE_URL).json(&body);
        if !odos_api_key.is_empty() {
            req = req.header("Authorization", format!("Bearer {}", odos_api_key));
        }

        match req.send().await {
            Ok(resp) if resp.status().is_success() => {
                match resp.json::<OdosAssembleResponse>().await {
                    Ok(assembled) => {
                        let hex = assembled.transaction.data.trim_start_matches("0x").to_string();
                        let bytes = hex::decode(&hex).unwrap_or_default();
                        println!("   ✅ Calldata assembled ({} bytes)", bytes.len());
                        bytes
                    }
                    Err(e) => {
                        println!("   ⚠️  Odos assemble parse error: {}", e);
                        vec![]
                    }
                }
            }
            _ => {
                println!("   ⚠️  Odos assemble failed. Empty calldata (dry-run safe).");
                vec![]
            }
        }
    } else {
        println!("   ⚠️  No path_id (test mode). Using empty calldata.");
        vec![]
    };

    // ── Step 2: ABI-encode the triggerLiquidation() call ─────────────────────
    let calldata = encode_trigger_liquidation(
        opportunity.target_user,
        opportunity.debt_asset,
        opportunity.collateral_asset,
        opportunity.debt_to_cover,
        odos_router,
        aggregator_data,
    );

    // ── Step 3: Dry run or live broadcast ─────────────────────────────────────
    if !live_mode {
        let hex_str = hex::encode(&calldata);
        let preview = format!("0x{}", &hex_str[..64_usize.min(hex_str.len())]);
        println!("   🟡 DRY RUN — NOT broadcasting (LIVE_MODE=false in .env)");
        println!("   📋 Calldata preview: {}...", preview);
        println!("   💰 Would earn: ${:.2} | From wallet: {:?}", opportunity.net_profit_usd, wallet_addr);
        return Ok(ExecutionResult::DryRun {
            calldata_preview: preview,
            estimated_profit: opportunity.net_profit_usd,
        });
    }

    // LIVE MODE — sign and broadcast raw transaction (avoids SignerMiddleware lifetime issues)
    println!("   🔴 LIVE MODE — Signing and broadcasting to Arbitrum...");

    let nonce = provider.get_transaction_count(wallet_addr, None).await?;
    let gas_price = provider.get_gas_price().await?;

    let mut tx_req = Eip1559TransactionRequest::new()
        .to(contract_address)
        .data(calldata.clone())
        .nonce(nonce)
        .max_priority_fee_per_gas(U256::from(100_000_000u64))
        .max_fee_per_gas(gas_price * 2);

    // Estimate gas
    let typed_for_estimate: TypedTransaction = tx_req.clone().into();
    if let Ok(gas_est) = provider.estimate_gas(&typed_for_estimate, None).await {
        tx_req = tx_req.gas(gas_est * 12 / 10); // 20% buffer
    } else {
        tx_req = tx_req.gas(U256::from(600_000u64));
    }

    // Sign locally with wallet — no middleware lifetime involved
    let typed: TypedTransaction = tx_req.into();
    let signature = wallet.sign_transaction(&typed).await
        .map_err(|e| eyre::eyre!("Signing failed: {}", e))?;
    let signed_rlp = typed.rlp_signed(&signature);

    // Broadcast raw signed bytes
    match provider.send_raw_transaction(signed_rlp).await {
        Ok(pending) => {
            let tx_hash = pending.tx_hash();
            println!("   ⏳ Broadcast: {:?} — waiting for confirmation...", tx_hash);
            match pending.await? {
                Some(receipt) => {
                    println!("   ✅ CONFIRMED: {:?}", receipt.transaction_hash);
                    println!("   💰 Profit: ${:.2}", opportunity.net_profit_usd);
                    Ok(ExecutionResult::Success {
                        tx_hash: receipt.transaction_hash,
                        profit_usd: opportunity.net_profit_usd,
                    })
                }
                None => Ok(ExecutionResult::Failed {
                    reason: "Tx dropped from mempool".to_string()
                })
            }
        }
        Err(e) => {
            println!("   ❌ Broadcast failed: {}", e);
            Ok(ExecutionResult::Failed { reason: e.to_string() })
        }
    }
}
