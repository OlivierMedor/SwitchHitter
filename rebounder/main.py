"""
Switch-Hitter: Uniswap V3 Rebound Tracker
Queries historical Uniswap V3 Pool slot0 prices to track the 'splash' (price dump)
and subsequent rebound following a massive liquidation.
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

# Uniswap V3 Factory on Arbitrum
FACTORY_ADDRESS = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
FACTORY_ABI = [{
    "inputs": [
        {"internalType": "address", "name": "", "type": "address"},
        {"internalType": "address", "name": "", "type": "address"},
        {"internalType": "uint24", "name": "", "type": "uint24"}
    ],
    "name": "getPool",
    "outputs": [{"internalType": "address", "name": "", "type": "address"}],
    "stateMutability": "view",
    "type": "function"
}]

POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Standard Uniswap V3 fee tiers
FEE_TIERS = [500, 3000, 100, 10000]

TOKEN_DECIMALS = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831".lower(): 6,   # USDC
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8".lower(): 6,   # USDC.e
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9".lower(): 6,   # USDT
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f".lower(): 8,   # WBTC
    "0xd8a4fdf445217bd4f877ce3df6ddb49bee04aeba".lower(): 2,   # EURS
}

def get_decimals(addr):
    return TOKEN_DECIMALS.get(addr.lower(), 18)

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME
    )

def find_best_pool(w3, factory, tokenA, tokenB):
    """Finds the Uniswap pool with the lowest fee tier (usually the most liquid for major pairs)"""
    for fee in FEE_TIERS:
        pool_addr = factory.functions.getPool(
            Web3.to_checksum_address(tokenA),
            Web3.to_checksum_address(tokenB),
            fee
        ).call()
        if pool_addr != "0x0000000000000000000000000000000000000000":
            return pool_addr
    return None

def get_pool_price(w3, pool_contract, block_num, col_asset):
    """
    Computes the price of col_asset in terms of debt_asset from slot0.
    """
    try:
        # Fetch slot0 at the specific block
        slot0 = pool_contract.functions.slot0().call(block_identifier=block_num)
        sqrtPriceX96 = slot0[0]
        
        # Token ordering matters in Uniswap V3
        token0 = pool_contract.functions.token0().call().lower()
        token1 = pool_contract.functions.token1().call().lower()
        
        # Math: Price of token1 in terms of token0 = (sqrtPriceX96 / 2**96)**2
        raw_price = (sqrtPriceX96 / (2**96)) ** 2
        
        dec0 = get_decimals(token0)
        dec1 = get_decimals(token1)
        
        # Adjust for decimal difference
        adjusted_price = raw_price * (10 ** (dec0 - dec1))
        
        # We want price of collateral in terms of debt
        col_asset_lower = col_asset.lower()
        if col_asset_lower == token1:
            return adjusted_price
        elif col_asset_lower == token0:
            if adjusted_price == 0: return 0
            return 1 / adjusted_price
        else:
            return 0
    except Exception as e:
        # E.g. pool didn't exist yet at that block, or Alchemy rate limit
        return None

def main():
    print("\n--- Switch-Hitter: Rebound Data Pipeline ---")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    factory = w3.eth.contract(
        address=Web3.to_checksum_address(FACTORY_ADDRESS),
        abi=FACTORY_ABI
    )

    conn = get_db_connection()
    cur = conn.cursor()

    # Fetch liquidations that haven't been tracked for rebounds yet
    # Focus on those where we actually had a viable swap (positive profit)
    cur.execute("""
        SELECT id, block_number, collateral_asset, debt_asset
        FROM liquidations
        WHERE status = 'enriched' 
          AND net_profit_usd > 0
          AND price_block_before IS NULL
        ORDER BY timestamp DESC
        LIMIT 1000; 
    """)
    rows = cur.fetchall()

    if not rows:
        print("No unchecked liquidations found for rebound tracking.")
        return

    print(f"Tracking rebounds for {len(rows)} liquidations...\n")
    processed = 0

    for row in rows:
        row_id, block_number, col_asset, debt_asset = row
        
        # Same-token pair
        if col_asset.lower() == debt_asset.lower():
            cur.execute("""UPDATE liquidations SET price_block_before=0, price_block_after=0, 
                           price_block_plus_10=0, price_block_plus_50=0 WHERE id=%s""", (row_id,))
            conn.commit()
            continue
            
        pool_addr = find_best_pool(w3, factory, col_asset, debt_asset)
        
        if not pool_addr:
            cur.execute("""UPDATE liquidations SET price_block_before=0, price_block_after=0, 
                           price_block_plus_10=0, price_block_plus_50=0 WHERE id=%s""", (row_id,))
            conn.commit()
            continue
            
        pool_contract = w3.eth.contract(address=pool_addr, abi=POOL_ABI)
        
        # Query the exact blocks
        price_before = get_pool_price(w3, pool_contract, block_number - 1, col_asset)
        price_after  = get_pool_price(w3, pool_contract, block_number, col_asset)
        price_10     = get_pool_price(w3, pool_contract, block_number + 10, col_asset)
        price_50     = get_pool_price(w3, pool_contract, block_number + 50, col_asset)
        
        if price_before is None or price_after is None:
            # Maybe Alchemy limits or pool too new
            print(f"  ID {row_id} [Block {block_number}]: Failed to fetch prices. Continuing...")
            time.sleep(0.5)
            continue
            
        cur.execute("""
            UPDATE liquidations 
            SET price_block_before = %s,
                price_block_after = %s,
                price_block_plus_10 = %s,
                price_block_plus_50 = %s
            WHERE id = %s
        """, (price_before, price_after, price_10, price_50, row_id))
        conn.commit()
        
        # Calculate the immediate "splash" and any rebound
        splash = ((price_after - price_before) / price_before * 100) if price_before > 0 else 0
        if price_after > 0 and price_50 is not None:
            rebound_50 = ((price_50 - price_after) / price_after * 100) 
        else:
            rebound_50 = 0
            
        print(f"  ID {row_id} | Splash: {splash:+.3f}% | 50-Block Rebound: {rebound_50:+.3f}%")
        
        processed += 1
        time.sleep(0.2) # Rate limit safety

    print(f"\nFinished tracking rebounds for {processed} liquidations.")
    conn.close()

if __name__ == "__main__":
    main()
