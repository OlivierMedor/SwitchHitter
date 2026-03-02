"""
Switch-Hitter: Uniswap V3 Slippage Quoter
Queries the QuoterV2 contract to get exact swap output for each historical liquidation,
replacing the static 0.50% slippage estimate with real on-chain data.
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

# Uniswap V3 QuoterV2 on Arbitrum
QUOTER_V2_ADDRESS = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

# QuoterV2 ABI - only the function we need
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

# Known token decimals
TOKEN_DECIMALS = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,   # USDC
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,   # USDC.e
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,   # USDT
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": 8,   # WBTC
    "0xd8a4fdf445217bd4f877ce3df6ddb49bee04aeba": 2,   # EURS
}

# Standard Uniswap V3 fee tiers to try (in order of liquidity on Arbitrum)
FEE_TIERS = [500, 3000, 100, 10000]  # 0.05%, 0.30%, 0.01%, 1.00%

def get_decimals(addr):
    return TOKEN_DECIMALS.get(addr.lower(), 18)

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME
    )

def quote_swap(quoter_contract, token_in, token_out, amount_in):
    """
    Try all Uniswap V3 fee tiers and return the best (highest output) quote.
    Returns (amountOut, fee_tier) or (None, None) if no pool exists.
    """
    best_out = 0
    best_fee = None

    for fee in FEE_TIERS:
        try:
            result = quoter_contract.functions.quoteExactInputSingle((
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                int(amount_in),
                fee,
                0  # sqrtPriceLimitX96 = 0 means no limit
            )).call()

            amount_out = result[0]
            if amount_out > best_out:
                best_out = amount_out
                best_fee = fee
        except Exception:
            # Pool doesn't exist for this fee tier, skip
            continue

    if best_out > 0:
        return best_out, best_fee
    return None, None


def main():
    print("\n--- Switch-Hitter: Uniswap V3 Slippage Quoter ---")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(QUOTER_V2_ADDRESS),
        abi=QUOTER_ABI
    )

    conn = get_db_connection()
    cur = conn.cursor()

    # Fetch enriched liquidations that haven't been quoted yet
    cur.execute("""
        SELECT id, collateral_asset, debt_asset, liquidated_collateral_amount, debt_to_cover
        FROM liquidations
        WHERE status = 'enriched' AND quoted_slippage_bps IS NULL
        ORDER BY id ASC;
    """)
    rows = cur.fetchall()

    if not rows:
        print("No unquoted liquidations found. All rows already have slippage data!")
        return

    print(f"Found {len(rows)} liquidations to quote.\n")

    quoted_count = 0
    skipped_count = 0

    for row in rows:
        row_id, col_asset, debt_asset, col_amount_raw, debt_amount_raw = row

        # The liquidation seizes collateral. Our bot would need to SWAP that collateral
        # back to the debt token to repay the flashloan.
        # So: tokenIn = collateral, tokenOut = debt, amountIn = liquidatedCollateralAmount
        amount_in = int(col_amount_raw)

        try:
            quoted_out, fee_tier = quote_swap(quoter, col_asset, debt_asset, amount_in)

            if quoted_out is None:
                print(f"  ID {row_id}: No Uniswap pool found for {col_asset[:6]}→{debt_asset[:6]}, skipping")
                skipped_count += 1
                # Mark as 0 slippage so we don't re-process
                cur.execute(
                    "UPDATE liquidations SET quoted_slippage_bps = -1, quoted_swap_output = 0 WHERE id = %s",
                    (row_id,)
                )
                conn.commit()
                continue

            # Calculate slippage: compare what Uniswap gives us vs. what we need to repay
            # debt_amount_raw = what we borrowed (must repay)
            # quoted_out = what Uniswap will give us for selling the collateral
            debt_amount = int(debt_amount_raw)

            if debt_amount > 0:
                # Slippage = how much less we get vs. what we need, as basis points
                # If quoted_out > debt_amount, we profit (negative slippage = good)
                # If quoted_out < debt_amount, we lose (positive slippage = bad)
                slippage_bps = ((debt_amount - quoted_out) / debt_amount) * 10000
            else:
                slippage_bps = 0

            # Save to database
            cur.execute(
                "UPDATE liquidations SET quoted_slippage_bps = %s, quoted_swap_output = %s WHERE id = %s",
                (round(slippage_bps, 4), str(quoted_out), row_id)
            )
            conn.commit()
            quoted_count += 1

            status = "✅ PROFIT" if slippage_bps < 0 else "⚠️ SLIP"
            print(f"  ID {row_id}: {status} | Slippage: {slippage_bps:.2f} bps | Fee: {fee_tier/10000:.2f}% | Out: {quoted_out}")

        except Exception as e:
            print(f"  ID {row_id}: ERROR - {e}")
            skipped_count += 1
            continue

        # Respect Alchemy rate limits
        time.sleep(0.1)

    print(f"\n=== QUOTER SUMMARY ===")
    print(f"Quoted: {quoted_count}")
    print(f"Skipped (no pool): {skipped_count}")
    print(f"Total: {len(rows)}")

    conn.close()


if __name__ == "__main__":
    main()
