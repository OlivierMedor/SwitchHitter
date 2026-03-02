"""
Switch-Hitter: Atomic Scavenger Arb Simulator
Simulates backrunning a massive Uniswap V3 liquidation dump by instantly buying the 
crashed collateral on Uniswap V3 and selling it on Sushiswap V3 in the same block.
"""
import os
import time
import psycopg2
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")
RPC_URL = os.getenv("ARBITRUM_RPC_URL")

# Both DEXs use the exact same QuoterV2 architecture on Arbitrum
UNISWAP_QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
SUSHISWAP_QUOTER = "0x89C04a0cbEfe7119AaCFf0561Cb5e3cBAebEdE2f"

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

FEE_TIERS = [500, 3000, 100, 10000]

TOKEN_DECIMALS = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,   # USDC
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,   # USDC.e
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,   # USDT
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": 8,   # WBTC
    "0xd8a4fdf445217bd4f877ce3df6ddb49bee04aeba": 2,   # EURS
}

def get_decimals(addr):
    return TOKEN_DECIMALS.get(addr.lower(), 18)

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME
    )

def fetch_historical_price(token_address, date_str):
    """Placeholder for DeFi Llama price fetcher - we'll just use a static $1 assumption for USDC/USDT for speed here since returning USD profit"""
    # For a true backtester we'd adapt the main.py logic, but here we just want relative profit in debt token out.
    pass

def quote_swap(quoter_contract, token_in, token_out, amount_in, block_num):
    best_out = 0
    best_fee = None
    for fee in FEE_TIERS:
        try:
            result = quoter_contract.functions.quoteExactInputSingle((
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                int(amount_in),
                fee,
                0
            )).call(block_identifier=block_num)
            
            amount_out = result[0]
            if amount_out > best_out:
                best_out = amount_out
                best_fee = fee
        except Exception:
            continue
            
    if best_out > 0:
        return best_out, best_fee
    return None, None

def main():
    print("\n--- Switch-Hitter: Atomic Scavenger Arb Simulator ---")
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    uni_quoter = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_QUOTER), abi=QUOTER_ABI)
    sushi_quoter = w3.eth.contract(address=Web3.to_checksum_address(SUSHISWAP_QUOTER), abi=QUOTER_ABI)

    conn = get_db_connection()
    cur = conn.cursor()

    # Target liquidations with a massive splash on Uniswap (> 1% drop)
    cur.execute("""
        SELECT id, block_number, collateral_asset, debt_asset, price_block_before, price_block_after, debt_to_cover
        FROM liquidations
        WHERE status = 'enriched' 
          AND price_block_before > 0 
          AND ((price_block_after - price_block_before) / price_block_before) < -0.01
        ORDER BY timestamp DESC
        LIMIT 100;
    """)
    rows = cur.fetchall()

    if not rows:
        print("No massive splashes found or all already simulated.")
        return

    print(f"Simulating Atomic Arb for {len(rows)} massive liquidations...\n")

    # The Strategy: We flashloan the EXACT amount of the original liquidation
    FLASHLOAN_FEE_BPS = 5 # 0.05% Aave fee

    for row in rows:
        row_id, block_num, col_asset, debt_asset, p_before, p_after, debt_to_cover = row
        
        dec_debt = get_decimals(debt_asset)
        dec_col = get_decimals(col_asset)
        
        # Borrow the exact amount the liquidator used
        borrow_amount_raw = int(debt_to_cover)
        if borrow_amount_raw == 0:
            continue
        
        # 1. Buy the crashed collateral on Uniswap V3 (where the dump just happened!)
        uni_col_out, uni_fee = quote_swap(uni_quoter, debt_asset, col_asset, borrow_amount_raw, block_num)
        
        if not uni_col_out:
            print(f"  ID {row_id} | Block {block_num}: No Uniswap liquidity to buy dip.")
            cur.execute("UPDATE liquidations SET scavenger_profit_usd = 0 WHERE id = %s", (row_id,))
            conn.commit()
            continue
            
        # 2. Instantly sell that collateral on Sushiswap V3 (where price hasn't dumped yet)
        sushi_debt_out, sushi_fee = quote_swap(sushi_quoter, col_asset, debt_asset, uni_col_out, block_num)
        
        if not sushi_debt_out:
            print(f"  ID {row_id} | Block {block_num}: No Sushiswap cross-dex liquidity.")
            cur.execute("UPDATE liquidations SET scavenger_profit_usd = 0 WHERE id = %s", (row_id,))
            conn.commit()
            continue
            
        # 3. Math Time
        repay_amount_raw = borrow_amount_raw + (borrow_amount_raw * FLASHLOAN_FEE_BPS // 10000)
        profit_raw = sushi_debt_out - repay_amount_raw
        
        # Convert profit to human float (assuming debt is roughly $1 USD peg for this simulation logging)
        # If it's WBTC or ETH debt, the profit is in that asset, but we log raw float.
        profit_float = profit_raw / (10 ** dec_debt)
        revenue_float = sushi_debt_out / (10 ** dec_debt)
        
        status = "✅ WIN" if profit_raw > 0 else "❌ LOSE"
        splash_pct = ((p_after - p_before) / p_before) * 100
        print(f"  ID {row_id} | Splash {splash_pct:.2f}% | {status} | Gross: ${revenue_float:.2f} | Net Profit: ${profit_float:.2f}")

        cur.execute(
            "UPDATE liquidations SET scavenger_revenue_usd = %s, scavenger_profit_usd = %s WHERE id = %s",
            (round(revenue_float, 2), round(profit_float, 2), row_id)
        )
        conn.commit()
        
        time.sleep(0.5)

if __name__ == "__main__":
    main()
