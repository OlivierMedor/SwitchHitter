"""
Switch-Hitter: Real-Time Execution Engine (M11)
Listens for massive liquidation splashes on Arbitrum. 
When detected, it runs a binary search against Sushiswap QuoterV2 to find the mathematically optimal flashloan size.
Finally, it constructs the payload to trigger `SwitchHitter.sol`.
"""
import os
import time
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("ARBITRUM_RPC_URL")
if not RPC_URL:
    raise ValueError("Missing ARBITRUM_RPC_URL in .env")

# --- ON-CHAIN TARGETS ---
# The address of the SwitchHitter.sol contract we scaffolded in M10 (Placeholder until deployed)
SWITCHHITTER_CONTRACT_ADDRESS = "0x0000000000000000000000000000000000000000" 

UNISWAP_V3_QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
SUSHISWAP_V3_QUOTER = "0x89C04a0cbEfe7119AaCFf0561Cb5e3cBAebEdE2f"

# Minimal ABI required to ask QuoterV2 for a price
QUOTER_ABI = [{
    "inputs": [{
        "components": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
        ],
        "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
        "name": "params",
        "type": "tuple"
    }],
    "name": "quoteExactInputSingle",
    "outputs": [
        {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
        {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
        {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
        {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}
    ],
    "stateMutability": "nonpayable",
    "type": "function"
}]

# Minimal ABI for SwitchHitter.sol to build the payload
SWITCHHITTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "debtAsset", "type": "address"},
            {"internalType": "address", "name": "collateralAsset", "type": "address"},
            {"internalType": "uint256", "name": "flashloanAmount", "type": "uint256"}
        ],
        "name": "executeScavengerArb",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

w3 = Web3(Web3.HTTPProvider(RPC_URL))
sushi_quoter = w3.eth.contract(address=Web3.to_checksum_address(SUSHISWAP_V3_QUOTER), abi=QUOTER_ABI)
uni_quoter = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V3_QUOTER), abi=QUOTER_ABI)

# Set a safety net for binary search execution to avoid infinite loops
MAX_BINARY_SEARCH_ITERATIONS = 10 
AAVE_FLASHLOAN_FEE_BPS = 5 # 0.05%

def quote_swap_amount(quoter, token_in, token_out, amount_in_wei):
    """Hits the Quoter contract to see how much we would get back from a specific DEX."""
    try:
        # Assuming standard 0.05% or 0.3% fee tier for simplicity in this MVP
        result = quoter.functions.quoteExactInputSingle((
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            int(amount_in_wei),
            500, # 0.05% pool fee
            0
        )).call()
        return result[0]
    except Exception:
        return 0

def binary_search_optimal_flashloan(debt_asset, collateral_asset, max_debt_to_borrow):
    """
    ALGORITHM: Binary Search for Maximum Profitable Flashloan 
    Instead of guessing a flat $10,000, we instantly ask the secondary DEX (Sushiswap) 
    how much it can absorb before slippage kills the profit. 
    """
    best_profit = 0
    optimal_borrow_amount = 0
    
    # Range of flashloan sizes to test: From 0 to the size of the massive dump itself.
    low = 0
    high = max_debt_to_borrow 
    
    print(f"  🔍 Starting Binary Search... Max Bound: {high}")
    
    for i in range(MAX_BINARY_SEARCH_ITERATIONS):
        mid = (low + high) // 2
        if mid == 0:
            break
            
        print(f"    Iter {i+1}: Trying Flashloan Size: {mid}")
            
        # 1. How much crashed collateral do we get if we buy `mid` amount on Uniswap V3?
        uni_col_received_wei = quote_swap_amount(uni_quoter, debt_asset, collateral_asset, mid)
        
        if uni_col_received_wei == 0:
            # Uniswap can't even handle this buy. Slash the borrow amount and loop again.
            high = mid - 1
            continue
            
        # 2. Instantly ask Sushiswap how much it would pay us to sell that exact collateral
        sushi_debt_received_wei = quote_swap_amount(sushi_quoter, collateral_asset, debt_asset, uni_col_received_wei)
        
        # 3. Calculate Flashloan repayment (Base + 0.05%)
        repayment_required = mid + (mid * AAVE_FLASHLOAN_FEE_BPS // 10000)
        
        # 4. Is the trade profitable?
        profit = sushi_debt_received_wei - repayment_required
        
        if profit > 0:
            # We are profitable! But can we borrow MORE and make MORE profit? Look higher.
            print(f"      ✅ Profitable! Profit: +{profit}. Looking higher...")
            if profit > best_profit:
                best_profit = profit
                optimal_borrow_amount = mid
            low = mid + 1
        else:
            # slippage killed us. The secondary DEX can't handle a trade this large. Look lower.
            print(f"      ❌ Loss! Net: {profit}. Slippage too high. Looking lower...")
            high = mid - 1
            
    return optimal_borrow_amount, best_profit

def build_transaction_payload(debt_asset, collateral_asset, optimal_amount):
    """Builds the encoded ABI payload that the off-chain Python engine sends to Arbitrum."""
    contract = w3.eth.contract(address=Web3.to_checksum_address(SWITCHHITTER_CONTRACT_ADDRESS), abi=SWITCHHITTER_ABI)
    
    encoded_data = contract.encodeABI(
        fn_name="executeScavengerArb",
        args=[
            Web3.to_checksum_address(debt_asset),
            Web3.to_checksum_address(collateralAsset),
            optimal_amount
        ]
    )
    return encoded_data

def main():
    print("🧠 Engine Online. Waiting for massive liquidations to strike...")
    
    # We simulate the Executor "waking up" when the Collector detects a $3,000,000 WBTC/USDC Dump
    # In M11 production, this is fed directly from the Collector's Redis/ZeroMQ stream instantly.
    
    mock_dump_size_usdc = 3000000 * 10**6 # $3m
    wbtc = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
    usdc = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
    
    print("\n🚨 CRASH DETECTED: $3M dump on WBTC/USDC pool on Uniswap V3.")
    print("⚙️ Handing off to Execution Engine to find max theoretical cross-DEX arb...")
    
    start_time = time.time()
    optimal_size, profit = binary_search_optimal_flashloan(usdc, wbtc, mock_dump_size_usdc)
    end_time = time.time()
    
    print(f"\n✅ BINARY SEARCH COMPLETE ({end_time - start_time:.3f} seconds)")
    
    if optimal_size > 0:
        print(f"👉 BEST FLASHLOAN SIZE: {optimal_size / 10**6:.2f} USDC")
        print(f"👉 EXPECTED PROFIT: {profit / 10**6:.2f} USDC")
        
        print("\n🚀 Building Transaction Payload for SwitchHitter.sol...")
        # payload = build_transaction_payload(usdc, wbtc, optimal_size)
        # print(f"Payload Data: {payload[:50]}...")
    else:
        print("❌ Trade aborted. Secondary DEX lacked liquidity to absorb any size of the splash.")

if __name__ == "__main__":
    main()
