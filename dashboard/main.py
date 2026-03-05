import os
import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")

# Setup page config
st.set_page_config(page_title="Switch-Hitter MEV Dashboard", layout="wide")

# Known Arbitrum Token Symbols
TOKEN_MAP = {
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "WETH",
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "USDC",
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": "USDC.e",
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": "USDT",
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": "WBTC",
    "0xd8a4fdf445217bd4f877ce3df6ddb49bee04aeba": "EURS",
    "0x912ce59144191c1204e64559fe8253a0e49e6548": "ARB",
    "0xf97f4df75154ae9c2e411b439c28fb3590cceba9": "LINK",
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": "DAI",
    "0x5979d7b546e38e414f7e9822514be5d22f03f5d9": "wstETH",
    "0xec70dcb4a1efa46b8f2d97c310c9c4790ba5ffa8": "rETH"
}

def get_symbol(address):
    if not address: return "UNKNOWN"
    return TOKEN_MAP.get(address.lower(), address[:6]+"..")

def init_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )

def fetch_data(conn):
    query = """
        SELECT 
            id, 
            tx_hash, 
            block_number, 
            timestamp, 
            collateral_asset,
            debt_asset,
            debt_to_cover, 
            status, 
            gas_used_units, 
            gas_cost_eth,
            competitor_attempts,
            net_profit_usd,
            quoted_slippage_bps,
            price_block_before,
            price_block_after,
            price_block_plus_10,
            price_block_plus_50,
            scavenger_revenue_usd,
            scavenger_profit_usd
        FROM liquidations
        ORDER BY timestamp DESC;
    """
    df = pd.read_sql(query, conn)
    return df

st.title("🥷 Switch-Hitter | MEV Radar")
st.markdown("Real-time tracker for Arbitrum Aave V3 Liquidation Calls and Profit Margins.")

conn = init_connection()

try:
    df = fetch_data(conn)
    
    if df.empty:
        st.info("No liquidations tracked yet. The Collector may still be syncing...")
    else:
        total_tracked = len(df)
        total_enriched = len(df[df['status'] == 'enriched'])
        
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        col1.metric("Total Tracked", total_tracked)
        col2.metric("Total Enriched", total_enriched)
        
        avg_gas = df['gas_cost_eth'].mean() if 'gas_cost_eth' in df and not df['gas_cost_eth'].isna().all() else 0
        col3.metric("Avg Gas Cost (ETH)", f"{avg_gas:.6f}")
        
        # Calculate raw overall profit
        total_profit = df['net_profit_usd'].sum() if 'net_profit_usd' in df and not df['net_profit_usd'].isna().all() else 0
        col4.metric("Raw Overall Profit", f"${total_profit:.2f}")
        
        # Filtered profit (only taking winning trades)
        profitable_mask = df['net_profit_usd'] > 0
        filtered_profit = df.loc[profitable_mask, 'net_profit_usd'].sum() if 'net_profit_usd' in df else 0
        col5.metric("Filtered Profit (Winners)", f"${filtered_profit:.2f}")
        
        # Avg real slippage (only for profitable trades we'd actually take)
        quoted = df.loc[profitable_mask, 'quoted_slippage_bps'].dropna()
        quoted = quoted[quoted >= 0]
        avg_slip = quoted.mean() / 100 if len(quoted) > 0 else None
        col6.metric("Avg Slippage (Winners)", f"{avg_slip:.3f}%" if avg_slip is not None else "Pending")
        
        if not df['timestamp'].isna().all():
            min_date = df['timestamp'].min().strftime('%b %d, %Y %H:%M')
            max_date = df['timestamp'].max().strftime('%b %d, %Y %H:%M')
            st.markdown(f"**🗓 Tracking Period:** {min_date} to {max_date} UTC")
        
        st.subheader("Recent Liquidations")
        
        # Format the dataframe for better display
        display_df = df.copy()
        
        # Format the timestamp
        display_df['timestamp'] = pd.to_datetime(display_df['timestamp']).dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # Calculate Splash and Rebound
        if 'price_block_before' in df and 'price_block_after' in df and 'price_block_plus_50' in df:
            # Splash: (After - Before) / Before
            display_df['splash_pct'] = display_df.apply(
                lambda row: ((row['price_block_after'] - row['price_block_before']) / row['price_block_before'] * 100) 
                            if pd.notnull(row['price_block_before']) and row['price_block_before'] > 0 else None, 
                axis=1
            )
            # Rebound: (Plus_50 - After) / After
            display_df['rebound_50_pct'] = display_df.apply(
                lambda row: ((row['price_block_plus_50'] - row['price_block_after']) / row['price_block_after'] * 100) 
                            if pd.notnull(row['price_block_after']) and pd.notnull(row['price_block_plus_50']) and row['price_block_after'] > 0 else None, 
                axis=1
            )
            
            # Format Splash 
            display_df['splash_pct'] = display_df['splash_pct'].apply(
                lambda x: f"{float(x):+.2f}%" if pd.notnull(x) else "-"
            )
            # Format Rebound
            display_df['rebound_50_pct'] = display_df['rebound_50_pct'].apply(
                lambda x: f"{float(x):+.2f}%" if pd.notnull(x) else "-"
            )
        else:
            display_df['splash_pct'] = "-"
            display_df['rebound_50_pct'] = "-"

        # Map addresses to human readable symbols
        if 'collateral_asset' in display_df and 'debt_asset' in display_df:
            display_df['collateral_asset'] = display_df['collateral_asset'].apply(get_symbol)
            display_df['debt_asset'] = display_df['debt_asset'].apply(get_symbol)
            # Reorder columns slightly for better reading
            cols = list(display_df.columns)
            display_df = display_df[['id', 'tx_hash', 'timestamp', 'collateral_asset', 'debt_asset', 'debt_to_cover', 'status', 'gas_cost_eth', 'competitor_attempts', 'quoted_slippage_bps', 'splash_pct', 'rebound_50_pct', 'net_profit_usd', 'scavenger_profit_usd']]

        # Format slippage as percentage (allow negative slippage, which is profitable)
        if 'quoted_slippage_bps' in display_df:
            display_df['quoted_slippage_bps'] = display_df['quoted_slippage_bps'].apply(
                lambda x: f"{float(x)/100:.3f}%" if pd.notnull(x) else "Pending"
            )
        
        # Convert very long wei values to raw string or abbreviated for the UI
        display_df['debt_to_cover'] = display_df['debt_to_cover'].apply(lambda x: f"{float(x):.2e}")
        
        # Format profit as a clean dollar string
        if 'net_profit_usd' in display_df:
            def format_profit(val):
                if pd.isna(val): return "Pending..."
                if float(val) == 0: return "Unpriceable"
                return f"${float(val):.2f}"
            display_df['net_profit_usd'] = display_df['net_profit_usd'].apply(format_profit)
            
        # Format Scavenger Profit
        if 'scavenger_profit_usd' in display_df:
            display_df['scavenger_profit_usd'] = display_df['scavenger_profit_usd'].apply(
                lambda x: f"${float(x):.2f}" if pd.notnull(x) else "-"
            )
        
        # Color code the status
        def color_status(val):
            color = 'green' if val == 'enriched' else 'orange'
            return f'color: {color}'
            
        def color_profit(val):
            if val == "Pending...":
                return 'color: gray'
            if val == "Unpriceable":
                return 'color: gray; font-style: italic'
            try:
                val_float = float(val.replace('$', '').replace(',', ''))
                color = 'green' if val_float > 0 else 'red'
                return f'color: {color}'
            except:
                return ''
            
        st.dataframe(
            display_df.style.map(color_status, subset=['status']).map(color_profit, subset=['net_profit_usd']),
            use_container_width=True,
            hide_index=True
        )
        
except Exception as e:
    st.error(f"Error fetching data: {e}")
