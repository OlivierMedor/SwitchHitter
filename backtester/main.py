import os
import requests
import psycopg2
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")

# Constants for Profit Calculation
AAVE_FLASH_LOAN_FEE_BPS = 9  # 0.09% Aave V3 default
DEX_SLIPPAGE_BPS = 50        # 0.50% standard assumption for huge swaps

# Known Arbitrum Token Decimals (Non-18)
TOKEN_DECIMALS = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": 6,  # Native USDC
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,  # Bridged USDC.e
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,  # USDT
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": 8,  # WBTC
    "0xd8a4fdf445217bd4f877ce3df6ddb49bee04aeba": 2,  # EURS
}

def get_decimals(token_address):
    """Fallback to 18 decimals for all standard WETH/ARB/LINK tokens"""
    return TOKEN_DECIMALS.get(token_address.lower(), 18)

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )

def fetch_historical_price(token_address, timestamp):
    """
    Fetches the historical USD price of a token on Arbitrum at a specific timestamp
    using the free DeFi Llama Pricing API.
    """
    # DeFi llama format: chain:address
    llama_id = f"arbitrum:{token_address}"
    url = f"https://coins.llama.fi/prices/historical/{timestamp}/{llama_id}"
    
    try:
        response = requests.get(url, timeout=10)
        # Fallback to current price if historical API crashes (e.g. simulator timestamps from 2026)
        if response.status_code != 200 or 'application/json' not in response.headers.get('content-type', '').lower():
            current_url = f"https://coins.llama.fi/prices/current/{llama_id}"
            response = requests.get(current_url, timeout=10)
            
        data = response.json()
        
        if 'coins' in data and llama_id in data['coins']:
            return data['coins'][llama_id]['price']
        else:
            print(f"[-] Warning: No historical price found for {llama_id} at {timestamp}")
            return None
    except Exception as e:
        print(f"[-] DefiLlama API Error: {e}")
        return None

def main():
    print("\n--- Switch-Hitter: Backtester v1 ---")
    print("Fetching Enriched records from database...\n")
    
    conn = get_db_connection()
    query = """
        SELECT 
            id, 
            tx_hash, 
            timestamp, 
            collateral_asset, 
            debt_asset, 
            debt_to_cover, 
            liquidated_collateral_amount,
            gas_used,
            competitor_attempts,
            quoted_slippage_bps
        FROM liquidations
        WHERE status = 'enriched'
        ORDER BY timestamp ASC;
    """
    
    df = pd.read_sql(query, conn)
    conn.close()
    
    if df.empty:
        print("No enriched liquidations found to analyze. Wait for the Collector and Enricher.")
        return
        
    total_liquidations = len(df)
    profitable_count = 0
    total_net_profit_usd = 0.0
    
    print(f"Found {total_liquidations} enriched liquidations. Analyzing...\n")
    
    for _, row in df.iterrows():
        # Convert Postgres datetime to explicit UNIX timestamp
        unix_ts = int(row['timestamp'].timestamp())
        
        # 1. Fetch exact historical pricing
        # We need the ETH price specifically for gas, so we use WETH on arbitrum
        weth_addr = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1" 
        
        col_price = fetch_historical_price(row['collateral_asset'], unix_ts)
        debt_price = fetch_historical_price(row['debt_asset'], unix_ts)
        eth_price = fetch_historical_price(weth_addr, unix_ts)
        
        if not col_price or not debt_price or not eth_price:
            print(f"Skipping ID {row['id']} due to missing price data.")
            continue
            
        # 2. Math Setup
        # Use exact decimals instead of universally defaulting to 1e18
        collateral_amt = float(row['liquidated_collateral_amount']) / (10 ** get_decimals(row['collateral_asset']))
        debt_amt = float(row['debt_to_cover']) / (10 ** get_decimals(row['debt_asset']))
        gas_amt_eth = float(row['gas_used'])
        
        # 3. Value calculations in USD
        gross_revenue_usd = collateral_amt * col_price
        debt_cost_usd = debt_amt * debt_price
        gas_cost_usd = gas_amt_eth * eth_price
        
        # 4. Flash loan & Slippage calculations
        flash_loan_fee_usd = debt_cost_usd * (AAVE_FLASH_LOAN_FEE_BPS / 10000.0)
        # Use real quoted slippage if available, otherwise fallback to static estimate
        real_slip = row.get('quoted_slippage_bps')
        if real_slip is not None and float(real_slip) >= 0:
            effective_slippage_bps = float(real_slip)
        else:
            effective_slippage_bps = DEX_SLIPPAGE_BPS
        slippage_cost_usd = gross_revenue_usd * (effective_slippage_bps / 10000.0)
        
        # 5. Final Net Profit
        total_costs_usd = debt_cost_usd + gas_cost_usd + flash_loan_fee_usd + slippage_cost_usd
        net_profit_usd = gross_revenue_usd - total_costs_usd
        
        # 6. Reporting
        status = "[PROFITABLE]" if net_profit_usd > 0 else "[UNPROFITABLE]"
        
        print(f"ID {row['id']} | Tx: {row['tx_hash'][:10]}...")
        print(f"  Revenue:  ${gross_revenue_usd:.2f} (Collateral Seized)")
        print(f"  Debt:    -${debt_cost_usd:.2f}")
        print(f"  FlashLn: -${flash_loan_fee_usd:.2f} (0.09%)")
        print(f"  Slip:    -${slippage_cost_usd:.2f} ({effective_slippage_bps/100:.2f}%)")
        print(f"  Gas:     -${gas_cost_usd:.2f}")
        print(f"  ------------------------------")
        print(f"  {status}: ${net_profit_usd:.2f}")
        print("")
        
        total_net_profit_usd += net_profit_usd
        if net_profit_usd > 0:
            profitable_count += 1
            
        # 7. Save to Database!
        try:
            update_conn = get_db_connection()
            with update_conn.cursor() as cur:
                cur.execute(
                    "UPDATE liquidations SET net_profit_usd = %s WHERE id = %s;",
                    (net_profit_usd, row['id'])
                )
                update_conn.commit()
            update_conn.close()
        except Exception as e:
            print(f"[-] Failed to save profit to DB for ID {row['id']}: {e}")
            
    print("=== SUMMARY STATS ===")
    print(f"Total Analyzed: {total_liquidations}")
    print(f"Profitable txns: {profitable_count} ({(profitable_count/total_liquidations)*100:.1f}%)")
    print(f"Total Net Pipeline Profit: ${total_net_profit_usd:.2f}")

if __name__ == "__main__":
    main()
