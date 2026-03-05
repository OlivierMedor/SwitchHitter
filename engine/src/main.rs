use ethers::prelude::*;
use ethers::types::{Address, Filter, H256, U256};
use eyre::Result;
use dotenv::dotenv;
use std::env;
use std::sync::Arc;
use std::collections::HashMap;
use tokio::sync::RwLock;

// Arbitrum Aave V3 Pool Contract Address
const AAVE_POOL_ADDRESS: &str = "0x794a61358D6845594F94dc1DB02A252b5b4814aD";

// The `Borrow` event topic0 keccak256 hash.
// This is the ABI signature: Borrow(address, address, address, uint256, uint8, uint256, uint16)
// We only care about this event to discover borrower wallet addresses.
const BORROW_TOPIC: &str = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0";

// Struct to hold a user's financial health data
#[derive(Debug, Clone)]
struct UserState {
    address: Address,
    health_factor: U256,
    total_collateral_base: U256,
    total_debt_base: U256,
}

// ABI for getUserAccountData(address) → returns 6 uint256 values
// We only need: totalCollateralBase, totalDebtBase, healthFactor
abigen!(
    IAavePool,
    r#"[function getUserAccountData(address user) external view returns (uint256 totalCollateralBase, uint256 totalDebtBase, uint256 availableBorrowsBase, uint256 currentLiquidationThreshold, uint256 ltv, uint256 healthFactor)]"#
);

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    println!("🚀 Starting Switch-Hitter V2 Execution Engine...");

    let rpc_url = env::var("QUICKNODE_WSS_URL").expect("QUICKNODE_WSS_URL must be set in .env");
    println!("🔌 Connecting to QuickNode WSS...");

    let ws = Ws::connect(&rpc_url).await?;
    let provider = Provider::new(ws);
    let client = Arc::new(provider);

    let block_number = client.get_block_number().await?;
    let chain_id = client.get_chainid().await?;

    println!("✅ Successfully connected to Arbitrum via QuickNode!");
    println!("⛓️  Chain ID: {}", chain_id);
    println!("📦 Current Block: {}", block_number);

    // === M21: STATE MATRIX ===
    // A thread-safe hashmap of all known Aave V3 borrowers
    // Key: Borrower's wallet address
    // Value: Their latest UserState (health factor, collateral, debt)
    let state_matrix: Arc<RwLock<HashMap<Address, UserState>>> = Arc::new(RwLock::new(HashMap::new()));

    // Build the Aave Pool contract handle (for getUserAccountData calls)
    let aave_pool_address: Address = AAVE_POOL_ADDRESS.parse()?;
    let aave_pool = IAavePool::new(aave_pool_address, Arc::clone(&client));

    println!("📡 Subscribing to Aave V3 Borrow events to build State Matrix...");

    // Subscribe to Aave V3 Borrow events from any block onwards
    let borrow_topic: H256 = BORROW_TOPIC.parse()?;
    let filter = Filter::new()
        .address(aave_pool_address)
        .topic0(borrow_topic);

    let mut log_stream = client.subscribe_logs(&filter).await?;

    println!("👁  Watching mempool for new borrowers...");

    while let Some(log) = log_stream.next().await {
        // The borrower's address is encoded as the 3rd topic (index 2) on a Borrow event.
        // Aave's Borrow event indexed params: onBehalfOf (address, indexed)
        if let Some(borrower_topic) = log.topics.get(2) {
            // Decode the bytes32 topic into a wallet address
            let borrower_address = Address::from(borrower_topic.0[12..].try_into().unwrap_or([0u8; 20]));

            // Skip zero-addresses (malformed logs)
            if borrower_address.is_zero() {
                continue;
            }

            println!("🔍 New borrower detected: {:?}", borrower_address);

            // Immediately query their current financial health from the blockchain
            match aave_pool.get_user_account_data(borrower_address).call().await {
                Ok((total_collateral, total_debt, _, _, _, health_factor)) => {
                    // Aave encodes the health factor as a uint256 with 18 decimals.
                    // A health factor of 1.0 = 1_000_000_000_000_000_000
                    let hf_display = health_factor.as_u128() as f64 / 1e18;

                    let user_state = UserState {
                        address: borrower_address,
                        health_factor,
                        total_collateral_base: total_collateral,
                        total_debt_base: total_debt,
                    };

                    // LIQUIDATION CHECK: Immediately flag anyone below HF 1.0!
                    let liquidation_threshold = U256::from(10u64).pow(U256::from(18));
                    if health_factor < liquidation_threshold {
                        println!("🚨 LIQUIDATION TARGET: {:?} | HF: {:.4} | COLLATERAL: {} | DEBT: {}",
                            borrower_address, hf_display, total_collateral, total_debt);
                    } else {
                        println!("  ✅ Healthy borrower: {:?} | HF: {:.4}", borrower_address, hf_display);
                    }

                    // Store in our in-memory State Matrix
                    let mut matrix = state_matrix.write().await;
                    matrix.insert(borrower_address, user_state);
                    println!("  📊 State Matrix size: {} tracked borrowers", matrix.len());
                }
                Err(e) => {
                    println!("  ⚠️  Failed to fetch account data for {:?}: {}", borrower_address, e);
                }
            }
        }
    }

    Ok(())
}
