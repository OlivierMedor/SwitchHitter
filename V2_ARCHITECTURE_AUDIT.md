# Switch-Hitter V2: Multi-Protocol MEV Liquidation Sniper
## Architecture & Strategy Audit Document

### 1. Executive Summary
This document outlines the architecture and execution strategy for "Switch-Hitter V2," a high-performance Maximal Extractable Value (MEV) bot operating on the Arbitrum network. The bot's primary objective is to execute zero-block, risk-free flashloan liquidations across multiple lending protocols (Aave V3, Radiant, Compound V3, Silo). 

### 2. Core Operational Strategy (Atomic Flashloan Liquidations)
The strategy relies on atomic transaction inclusion. We assume zero market risk and require zero starting capital.
1. Identify an underwater borrower (Health Factor < 1.0).
2. Borrow the required user debt via a Flashloan.
3. Repay the debt on behalf of the user to seize their collateral + a liquidation bonus (e.g., 5-10%).
4. Sell the seized collateral immediately via a DEX Aggregator.
5. Repay the Flashloan + Flashloan Fee.
6. Keep the remaining balance as pure profit.

**Safety Guarantee:** If step 4 yields less capital than required for step 5, the entire transaction atomically reverts, costing only minimal gas fees (fractions of a cent on Arbitrum).

### 3. System Architecture
The system is divided into two strict domains: the "Brain" (Off-Chain Execution Engine) and the "Hands" (On-Chain Smart Contract).

#### 3.1 Off-Chain Execution Engine (The "Brain")
*   **Hosting:** AWS `us-east-1` (N. Virginia) EC2 Ubuntu instance. This provides literal physical colocation (1-2ms latency) with the Arbitrum Sequencer and premium RPC provider routing hubs.
*   **Language:** Rust. Chosen for deterministic ultra-low latency, memory safety, and zero garbage collection pauses.
*   **RPC Provider:** QuickNode (Discover Tier). Chosen for 99.99% uptime, reliable WebSocket (WSS) streaming, and low-latency Arbitrum mempool access.
*   **The "Local State" Matrix:** On boot, the Engine queries the blockchain to load the debt/collateral balances of all users across supported protocols into RAM. 
*   **Event-Driven Triggers:** The Engine listens to the mempool via WSS. It does NOT constantly poll. It only recalculates state when it intercepts:
    1.  Chainlink Oracle Price updates.
    2.  User collateral withdrawals.
    3.  User debt increases.
*   **Profitability Sorting:** Upon intercepting a Chainlink price drop in the mempool, the Engine calculates Health Factors locally using the intercepted price. For all newly bankrupt users, it calculates expected profit (incorporating 1inch slippage quotes and gas fees). It drops negative-profit users and sorts the rest highest-to-lowest.
*   **Multi-Wallet Concurrency:** To avoid EVM Nonce blocking caused by a single failed transaction, the Engine controls a fleet of multiple externally owned accounts (EOAs/Wallets). It assigns the most profitable targets to different wallets, firing them at the smart contract simultaneously.

#### 3.2 DEX Aggregator Integration
*   **Provider:** 1inch API.
*   **Rationale:** Rather than hardcoding specific DEX logic (Uniswap V3, Camelot) into the Smart Contract, the Rust Engine queries the 1inch API off-chain: *"How do I sell 50 WBTC for USDC with minimal slippage right now?"*
*   1inch returns optimized, multi-path hex `calldata`. The Engine passes this `calldata` as a payload to our Smart Contract, which blindly executes it, guaranteeing the absolute best market execution across all available Arbitrum liquidity pools.

#### 3.3 On-Chain Smart Contract (`SwitchHitterV2.sol`)
*   **Role:** An inert, logic-less execution blueprint. It performs no complex math or state checks. It simply acts as the flashloan receiver, executes the liquidation, executes the 1inch swap payload, and checks profitability at the end.
*   **Flashloan Fallback System:**
    *   *Primary Route (0% Fee):* The contract dynamically checks the Balancer Vault's liquidity. If sufficient, it borrows from Balancer for exactly 0% fee.
    *   *Secondary Route (0.05% Fee):* If Balancer is drained, the contract seamlessly falls back to Aave V3.
*   **Profit Routing:** After the flashloan is conditionally repaid, residual profit is handled intelligently (e.g., 20% transferred to a cold storage address, 80% automatically deposited back into Aave's `aUSDC` yield pool to compound passively).

### 4. Step-by-Step Development Roadmap
*   **Phase 1: Smart Contract Deployment (Completed)**
    *   Developed `SwitchHitterV2.sol` featuring the Balancer->Aave fallback, 1inch calldata routing, and profit splitting.
*   **Phase 2: Rust Core & State Sync (Next Step)**
    *   Scaffold the Cargo project.
    *   Implement QuickNode WSS connection with automatic ping/reconnect state-machine logic.
    *   Fetch initial Aave user state via The Graph or RPC batching and store it in high-speed RAM hashmaps.
*   **Phase 3: Event Listeners & Math**
    *   Implement ABI decoders for Chainlink Aggregator events.
    *   Implement the Health Factor calculation `(Collateral * Price * LiqThreshold) / Debt`.
*   **Phase 4: API & Execution Bridging**
    *   Integrate 1inch Swap API to fetch `calldata` for profitable targets.
    *   Build Ethereum transaction signing using local private keys for the multi-wallet fleet.
    *   Deploy `SwitchHitterV2.sol` to Arbitrum Mainnet and test execution with microscopic amounts.

### 5. Known Risks & Technical Mitigations
*   **RPC Downtime/De-sync:** Addressed via the Rust Engine's state-machine. If WSS drops, state is set to `OFFLINE` (halting trading). Upon reconnect, it requests missing blocks, fast-forwards state, then resumes.
*   **DEX Slippage / Shallow Liquidity:** Addressed by the 1inch API integration and calculating expected slippage locally *before* firing. Targets where slippage destroys the profit margin are automatically abandoned.
*   **Transaction Reversion Gridlock:** Addressed by executing one target per transaction and isolating batch targets across multiple independent wallets in the fleet, bypassing nonce sequentiality failures.
