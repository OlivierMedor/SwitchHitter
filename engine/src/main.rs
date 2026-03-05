use ethers::prelude::*;
use ethers::types::{Address, BlockNumber, Filter, H256, U256, I256};
use eyre::Result;
use dotenv::dotenv;
use std::env;
use std::sync::Arc;
use std::collections::HashMap;
use tokio::sync::RwLock;

// ─────────────────────────────────────────────
//  CONTRACT ADDRESSES (Arbitrum Mainnet)
// ─────────────────────────────────────────────
const AAVE_POOL_ADDRESS: &str    = "0x794a61358D6845594F94dc1DB02A252b5b4814aD";
const AAVE_DEPLOY_BLOCK: u64     = 7_742_429; // Block Aave V3 was deployed on Arbitrum

// Chainlink ETH/USD price feed on Arbitrum
const ETH_USD_FEED: &str = "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612";
// Chainlink WBTC/USD price feed on Arbitrum
const WBTC_USD_FEED: &str = "0x6ce185539ad4fdaecd7274b9f5cb84734820e7bc";
// Chainlink USDC/USD price feed on Arbitrum
const USDC_USD_FEED: &str = "0x50834F3163758fcC1Df9973b6e91f0F0F0434aD3";

// ─────────────────────────────────────────────
//  EVENT TOPIC HEX SIGNATURES
// ─────────────────────────────────────────────

// Aave V3: Borrow(address reserve, address user, address onBehalfOf, ...)
const AAVE_BORROW_TOPIC: &str =
    "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0";

// Chainlink AggregatorV3: AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
const CHAINLINK_ANSWER_UPDATED_TOPIC: &str =
    "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f";

// ─────────────────────────────────────────────
//  STATE MATRIX DATA STRUCTURES
// ─────────────────────────────────────────────

/// Full snapshot of a single user's financial position across ALL their Aave assets.
/// Pre-loaded into RAM so we don't need any RPC calls at execution time.
#[derive(Debug, Clone)]
struct UserState {
    address:         Address,
    health_factor:   U256,          // Aave 18-decimal HF (1e18 = HF of 1.0)
    total_collateral_base: U256,    // Total collateral value in USD (8 decimals)
    total_debt_base:  U256,         // Total debt value in USD (8 decimals)
    last_updated_block: u64,
}

impl UserState {
    /// Quick helper: returns true if this user is currently liquidatable
    fn is_liquidatable(&self) -> bool {
        let one = U256::from(10u64).pow(U256::from(18));
        self.health_factor < one && !self.health_factor.is_zero()
    }

    /// Display the health factor as a human readable float (e.g. 1.3421)
    fn hf_display(&self) -> f64 {
        self.health_factor.as_u128() as f64 / 1e18
    }
}

// The main shared data structure: address → full user state
type StateMatrix = Arc<RwLock<HashMap<Address, UserState>>>;

// ─────────────────────────────────────────────
//  AAVE POOL ABI (minimal — only what we need)
// ─────────────────────────────────────────────
abigen!(
    IAavePool,
    r#"[{"inputs":[{"internalType":"address","name":"user","type":"address"}],"name":"getUserAccountData","outputs":[{"internalType":"uint256","name":"totalCollateralBase","type":"uint256"},{"internalType":"uint256","name":"totalDebtBase","type":"uint256"},{"internalType":"uint256","name":"availableBorrowsBase","type":"uint256"},{"internalType":"uint256","name":"currentLiquidationThreshold","type":"uint256"},{"internalType":"uint256","name":"ltv","type":"uint256"},{"internalType":"uint256","name":"healthFactor","type":"uint256"}],"stateMutability":"view","type":"function"}]"#
);

// ─────────────────────────────────────────────
//  MAIN ENTRY POINT
// ─────────────────────────────────────────────
#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    println!("🚀 Switch-Hitter V2 Execution Engine — M22 Build");
    println!("══════════════════════════════════════════════════");

    let rpc_url = env::var("QUICKNODE_WSS_URL").expect("QUICKNODE_WSS_URL must be set in .env");

    println!("🔌 Connecting to QuickNode WSS...");
    let ws = Ws::connect(&rpc_url).await?;
    let provider = Provider::new(ws).interval(std::time::Duration::from_millis(100));
    let client = Arc::new(provider);

    let block_number = client.get_block_number().await?;
    println!("✅ Connected | Chain: 42161 (Arbitrum) | Block: {}", block_number);
    println!();

    // Initialize the shared State Matrix
    let state_matrix: StateMatrix = Arc::new(RwLock::new(HashMap::new()));

    // ── PHASE A: HISTORICAL BACKFILL ─────────────────────────────────────────
    println!("📦 Phase A: Historical Backfill");
    println!("   Fetching all Aave V3 Borrow events since deployment...");
    println!("   This may take 1-2 minutes on first run.");

    let backfill_count = run_historical_backfill(
        Arc::clone(&client),
        Arc::clone(&state_matrix),
        block_number.as_u64().saturating_sub(50_000), // Last ~50k blocks (~2 weeks on Arbitrum)
        block_number.as_u64(),
    ).await?;

    println!("✅ Backfill complete! Loaded {} unique borrowers into State Matrix.", backfill_count);
    println!();

    // ── PHASE B: LIVE SUBSCRIPTIONS (run concurrently) ───────────────────────
    println!("📡 Phase B: Live Event Subscriptions");
    println!("   Listening for: Aave Borrow events + Chainlink Oracle updates");
    println!();

    // Clone all shared resources for each spawned task
    let sm_for_borrow   = Arc::clone(&state_matrix);
    let sm_for_chainlink = Arc::clone(&state_matrix);
    let client_for_borrow   = Arc::clone(&client);
    let client_for_chainlink = Arc::clone(&client);

    // Spawn Task 1: Listen for new Aave Borrow events (add new borrowers to State Matrix)
    let borrow_task = tokio::spawn(async move {
        if let Err(e) = listen_for_new_borrows(client_for_borrow, sm_for_borrow).await {
            eprintln!("⚠️  Borrow listener error: {}", e);
        }
    });

    // Spawn Task 2: Listen for Chainlink price oracle updates (the main trigger)
    let chainlink_task = tokio::spawn(async move {
        if let Err(e) = listen_for_oracle_updates(client_for_chainlink, sm_for_chainlink).await {
            eprintln!("⚠️  Chainlink listener error: {}", e);
        }
    });

    // Run both tasks concurrently until one fails or the engine is stopped
    tokio::try_join!(borrow_task, chainlink_task)?;

    Ok(())
}

// ─────────────────────────────────────────────
//  PHASE A: HISTORICAL BACKFILL
//  Chunks eth_getLogs calls to avoid hitting QuickNode's block range limit
// ─────────────────────────────────────────────
async fn run_historical_backfill(
    client: Arc<Provider<Ws>>,
    state_matrix: StateMatrix,
    from_block: u64,
    to_block: u64,
) -> Result<usize> {
    let aave_address: Address = AAVE_POOL_ADDRESS.parse()?;
    let borrow_topic: H256 = AAVE_BORROW_TOPIC.parse()?;

    // QuickNode free (Discover) plan: max 5 blocks per eth_getLogs call
    // Upgrade to Build ($49/mo) for 10,000+ block ranges
    let chunk_size: u64 = 5;
    let mut start = from_block;
    let mut unique_borrowers: std::collections::HashSet<Address> = std::collections::HashSet::new();

    while start <= to_block {
        let end = (start + chunk_size - 1).min(to_block);

        let filter = Filter::new()
            .address(aave_address)
            .topic0(borrow_topic)
            .from_block(BlockNumber::Number(start.into()))
            .to_block(BlockNumber::Number(end.into()));

        match client.get_logs(&filter).await {
            Ok(logs) => {
                for log in &logs {
                    if let Some(topic) = log.topics.get(2) {
                        let addr = Address::from(topic.0[12..].try_into().unwrap_or([0u8; 20]));
                        if !addr.is_zero() {
                            unique_borrowers.insert(addr);
                        }
                    }
                }
                if !logs.is_empty() {
                    print!("\r   Scanning blocks {}-{} | {} unique borrowers found...", start, end, unique_borrowers.len());
                }
            }
            Err(e) => {
                eprintln!("\n   ⚠️  Chunk {}-{} failed: {}. Skipping...", start, end, e);
            }
        }

        start = end + 1;
        // Small delay to be gentle on the free tier rate limits
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }

    println!("\n   Fetching account data for {} borrowers...", unique_borrowers.len());

    // Now bulk-query getUserAccountData for all discovered addresses
    let aave_pool = IAavePool::new(AAVE_POOL_ADDRESS.parse::<Address>()?, Arc::clone(&client));
    let mut loaded = 0usize;

    for address in unique_borrowers {
        match aave_pool.get_user_account_data(address).call().await {
            Ok((total_collateral, total_debt, _, _, _, health_factor)) => {
                // Only track users with actual active debt (skip zero-debt wallets)
                if total_debt.is_zero() {
                    continue;
                }

                let user_state = UserState {
                    address,
                    health_factor,
                    total_collateral_base: total_collateral,
                    total_debt_base: total_debt,
                    last_updated_block: 0, // Will be updated on next event
                };

                let mut matrix = state_matrix.write().await;
                matrix.insert(address, user_state);
                loaded += 1;
            }
            Err(_) => {} // Skip failed queries silently during backfill
        }
    }

    Ok(loaded)
}

// ─────────────────────────────────────────────
//  PHASE B, TASK 1: Live Borrow Event Listener
//  Adds newly detected borrowers to the State Matrix in real-time
// ─────────────────────────────────────────────
async fn listen_for_new_borrows(
    client: Arc<Provider<Ws>>,
    state_matrix: StateMatrix,
) -> Result<()> {
    let aave_address: Address = AAVE_POOL_ADDRESS.parse()?;
    let borrow_topic: H256 = AAVE_BORROW_TOPIC.parse()?;
    let aave_pool = IAavePool::new(aave_address, Arc::clone(&client));

    let filter = Filter::new().address(aave_address).topic0(borrow_topic);
    let mut stream = client.subscribe_logs(&filter).await?;

    println!("👁  Borrow Listener: Active and watching...");

    while let Some(log) = stream.next().await {
        if let Some(topic) = log.topics.get(2) {
            let borrower = Address::from(topic.0[12..].try_into().unwrap_or([0u8; 20]));
            if borrower.is_zero() { continue; }

            // Fetch and update this user's health in the State Matrix
            if let Ok((collateral, debt, _, _, _, hf)) =
                aave_pool.get_user_account_data(borrower).call().await
            {
                let block = log.block_number.map(|b| b.as_u64()).unwrap_or(0);
                let user = UserState {
                    address: borrower,
                    health_factor: hf,
                    total_collateral_base: collateral,
                    total_debt_base: debt,
                    last_updated_block: block,
                };

                let hf_display = user.hf_display();
                let is_target = user.is_liquidatable();

                let mut matrix = state_matrix.write().await;
                matrix.insert(borrower, user);
                let size = matrix.len();
                drop(matrix);

                if is_target {
                    println!("🚨 NEW LIQUIDATION TARGET via Borrow: {:?} | HF: {:.4}", borrower, hf_display);
                } else {
                    println!("➕ New borrower added: {:?} | HF: {:.4} | Matrix: {} users", borrower, hf_display, size);
                }
            }
        }
    }

    Ok(())
}

// ─────────────────────────────────────────────
//  PHASE B, TASK 2: Chainlink Oracle Listener (THE MAIN TRIGGER)
//  When any Chainlink price feed updates, scan the ENTIRE State Matrix
//  and look for users that just dropped below HF 1.0
// ─────────────────────────────────────────────
async fn listen_for_oracle_updates(
    client: Arc<Provider<Ws>>,
    state_matrix: StateMatrix,
) -> Result<()> {
    // Monitor all three major price feeds simultaneously
    let feeds: Vec<Address> = vec![
        ETH_USD_FEED.parse()?,
        WBTC_USD_FEED.parse()?,
        USDC_USD_FEED.parse()?,
    ];

    let feed_names = vec!["ETH/USD", "WBTC/USD", "USDC/USD"];

    let update_topic: H256 = CHAINLINK_ANSWER_UPDATED_TOPIC.parse()?;
    let aave_pool = IAavePool::new(AAVE_POOL_ADDRESS.parse::<Address>()?, Arc::clone(&client));

    let filter = Filter::new()
        .address(feeds.clone())
        .topic0(update_topic);

    let mut stream = client.subscribe_logs(&filter).await?;

    println!("⚡ Chainlink Listener: Monitoring ETH/USD, WBTC/USD, USDC/USD...");

    while let Some(log) = stream.next().await {
        // Identify which price feed just updated
        let feed_name = feeds.iter().zip(feed_names.iter())
            .find(|(addr, _)| log.address == **addr)
            .map(|(_, name)| *name)
            .unwrap_or("UNKNOWN");

        // Decode the new price from the first indexed topic (int256)
        let new_price_raw = if let Some(topic) = log.topics.get(1) {
            I256::from_raw(topic.into_uint())
        } else {
            continue;
        };

        let new_price_usd = new_price_raw.as_i128() as f64 / 1e8; // Chainlink uses 8 decimals
        let block = log.block_number.map(|b| b.as_u64()).unwrap_or(0);

        println!("⚡ ORACLE UPDATE: {} = ${:.2} (Block: {})", feed_name, new_price_usd, block);
        println!("   🔍 Scanning entire State Matrix for newly liquidatable users...");

        let matrix = state_matrix.read().await;
        let matrix_size = matrix.len();
        drop(matrix);

        // Re-query ALL users from Aave using the new on-chain price
        // NOTE: In M23 we will replace this with pure local math (no RPC call)
        // For now, these are parallel async calls — fast but not zero-latency
        let matrix_read = state_matrix.read().await;
        let addresses: Vec<Address> = matrix_read.keys().cloned().collect();
        drop(matrix_read);

        let mut targets: Vec<(Address, f64)> = Vec::new();

        for address in &addresses {
            if let Ok((_, _, _, _, _, hf)) =
                aave_pool.get_user_account_data(*address).call().await
            {
                let hf_display = hf.as_u128() as f64 / 1e18;
                let one_e18 = U256::from(10u64).pow(U256::from(18));

                if hf < one_e18 && !hf.is_zero() {
                    targets.push((*address, hf_display));
                }

                // Update state matrix with fresh HF
                let mut matrix = state_matrix.write().await;
                if let Some(user) = matrix.get_mut(address) {
                    user.health_factor = hf;
                    user.last_updated_block = block;
                }
            }
        }

        if targets.is_empty() {
            println!("   ✅ No liquidations triggered. All {} users still healthy.", matrix_size);
        } else {
            println!("   🚨 {} LIQUIDATION TARGETS DETECTED after {} update!", targets.len(), feed_name);

            // Sort by HF ascending (most underwater = most urgent)
            targets.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap());

            for (addr, hf) in &targets {
                println!("      💀 {:?} | HF: {:.4} — QUEUING FOR EXECUTION", addr, hf);
            }

            // TODO (M23): Pass `targets` to the 1inch Aggregator + Profit Sorter
            // TODO (M24): Fire triggerLiquidation() via the multi-wallet fleet
        }

        println!();
    }

    Ok(())
}
