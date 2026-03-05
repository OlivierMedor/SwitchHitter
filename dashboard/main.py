import os
import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")

st.set_page_config(page_title="Switch-Hitter V2 | MEV Bot", layout="wide", page_icon="🥷")

TOKEN_MAP = {
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "WETH",
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "USDC",
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": "USDC.e",
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": "USDT",
    "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f": "WBTC",
    "0x912ce59144191c1204e64559fe8253a0e49e6548": "ARB",
    "0xf97f4df75154ae9c2e411b439c28fb3590cceba9": "LINK",
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": "DAI",
    "0x5979d7b546e38e414f7e9822514be5d22f03f5d9": "wstETH",
    "0xec70dcb4a1efa46b8f2d97c310c9c4790ba5ffa8": "rETH"
}

PROTOCOL_MAP = {
    "aave_v3": "Aave V3",
    "radiant": "Radiant",
    "compound": "Compound V3",
    "silo": "Silo"
}

def get_symbol(address):
    if not address: return "UNKNOWN"
    return TOKEN_MAP.get(address.lower(), address[:8] + "..")

def init_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASSWORD, database=DB_NAME
    )

def fetch_historical_liquidations(conn):
    query = """
        SELECT id, tx_hash, timestamp, collateral_asset, debt_asset,
               debt_to_cover, status, gas_cost_eth, competitor_attempts,
               net_profit_usd, quoted_slippage_bps
        FROM liquidations
        ORDER BY timestamp DESC
        LIMIT 500;
    """
    return pd.read_sql(query, conn)

def fetch_bot_executions(conn):
    query = """
        SELECT attempted_at, target_user, protocol, debt_asset, collateral_asset,
               flashloan_provider, health_factor_at_trigger, expected_profit_usd,
               actual_profit_usd, gas_cost_eth, status, wallet_used, tx_hash
        FROM bot_executions
        ORDER BY attempted_at DESC
        LIMIT 200;
    """
    return pd.read_sql(query, conn)

# ──────────────────────────────────────────────────────────
# HEADER
# ──────────────────────────────────────────────────────────
st.title("🥷 Switch-Hitter V2 | MEV Liquidation Bot")
st.markdown("*Arbitrum Multi-Protocol Liquidation Sniper — Aave V3 · Radiant · Compound · Silo*")
st.divider()

conn = init_connection()

try:
    # ──────────────────────────────────────────────────────
    # TAB LAYOUT
    # ──────────────────────────────────────────────────────
    tab1, tab2 = st.tabs(["📡 Live Bot Performance", "📜 Historical Liquidation Radar"])

    # ──────────────────────────────────────────────────────
    # TAB 1: LIVE BOT PERFORMANCE (V2 Execution Engine)
    # ──────────────────────────────────────────────────────
    with tab1:
        st.subheader("🤖 Execution Engine Status")

        try:
            exec_df = fetch_bot_executions(conn)

            if exec_df.empty:
                st.info("🟡 Rust Execution Engine is not yet live. No bot executions recorded.")
                st.markdown("""
                **Engine Status:** Building Phase (M20-M24)

                The V2 Rust Execution Engine is currently under active development.
                Once deployed, this tab will display:
                - Live execution attempts and results per protocol
                - Wallet fleet performance and balances
                - Win/loss rate against competing bots
                - Gas efficiency and flashloan provider breakdown
                """)
            else:
                total_attempts = len(exec_df)
                wins = len(exec_df[exec_df['status'] == 'success'])
                losses = len(exec_df[exec_df['status'] == 'failed'])
                beaten = len(exec_df[exec_df['status'] == 'beaten'])
                win_rate = (wins / total_attempts * 100) if total_attempts > 0 else 0

                total_profit = exec_df['actual_profit_usd'].sum() if not exec_df['actual_profit_usd'].isna().all() else 0
                total_gas = exec_df['gas_cost_eth'].sum() if not exec_df['gas_cost_eth'].isna().all() else 0

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Total Attempts", total_attempts)
                c2.metric("✅ Wins", wins, f"{win_rate:.1f}% rate")
                c3.metric("❌ Beaten by Competitor", beaten)
                c4.metric("Net Profit (USD)", f"${total_profit:,.2f}")
                c5.metric("Total Gas Spent (ETH)", f"{total_gas:.5f}")

                st.divider()
                st.subheader("Execution Log")

                display = exec_df.copy()
                display['debt_asset'] = display['debt_asset'].apply(get_symbol)
                display['collateral_asset'] = display['collateral_asset'].apply(get_symbol)
                display['protocol'] = display['protocol'].map(PROTOCOL_MAP).fillna(display['protocol'])
                display['actual_profit_usd'] = display['actual_profit_usd'].apply(
                    lambda x: f"${x:,.2f}" if pd.notnull(x) else "-"
                )

                def color_status(val):
                    colors = {'success': 'green', 'beaten': 'orange', 'failed': 'red', 'pending': 'gray'}
                    return f"color: {colors.get(val, 'white')}"

                st.dataframe(
                    display.style.map(color_status, subset=['status']),
                    use_container_width=True, hide_index=True
                )

        except Exception as e:
            st.warning(f"bot_executions table not yet created. Run `docker-compose up` with the new schema to activate. ({e})")

    # ──────────────────────────────────────────────────────
    # TAB 2: HISTORICAL RADAR (Phase 1 Data - Reference Only)
    # ──────────────────────────────────────────────────────
    with tab2:
        st.subheader("📜 Historical Aave V3 Liquidation Radar")
        st.caption("Phase 1 historical data — used for backtesting and opportunity analysis. This is NOT live bot execution data.")

        hist_df = fetch_historical_liquidations(conn)

        if hist_df.empty:
            st.info("No historical liquidations tracked yet.")
        else:
            total = len(hist_df)
            enriched = len(hist_df[hist_df['status'] == 'enriched'])
            total_profit = hist_df['net_profit_usd'].sum() if not hist_df['net_profit_usd'].isna().all() else 0
            winning = hist_df[hist_df['net_profit_usd'] > 0]
            win_profit = winning['net_profit_usd'].sum() if not winning.empty else 0
            avg_gas = hist_df['gas_cost_eth'].mean() if not hist_df['gas_cost_eth'].isna().all() else 0
            avg_slip = hist_df.loc[hist_df['net_profit_usd'] > 0, 'quoted_slippage_bps'].dropna()
            avg_slip = avg_slip[avg_slip >= 0].mean() / 100 if len(avg_slip) > 0 else None

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Total Tracked", total)
            c2.metric("Enriched", enriched)
            c3.metric("Gross Profit (Winners)", f"${win_profit:,.2f}")
            c4.metric("Avg Gas (ETH)", f"{avg_gas:.6f}")
            c5.metric("Avg Slippage (Winners)", f"{avg_slip:.3f}%" if avg_slip else "—")

            if not hist_df['timestamp'].isna().all():
                min_d = pd.to_datetime(hist_df['timestamp']).min().strftime('%b %d, %Y')
                max_d = pd.to_datetime(hist_df['timestamp']).max().strftime('%b %d, %Y')
                st.caption(f"📅 Data range: {min_d} → {max_d}")

            display = hist_df.copy()
            display['timestamp'] = pd.to_datetime(display['timestamp']).dt.strftime('%Y-%m-%d %H:%M')
            display['collateral_asset'] = display['collateral_asset'].apply(get_symbol)
            display['debt_asset'] = display['debt_asset'].apply(get_symbol)
            display['debt_to_cover'] = display['debt_to_cover'].apply(lambda x: f"{float(x):.2e}")
            display['quoted_slippage_bps'] = display['quoted_slippage_bps'].apply(
                lambda x: f"{float(x)/100:.3f}%" if pd.notnull(x) else "Pending"
            )
            display['net_profit_usd'] = display['net_profit_usd'].apply(
                lambda x: "Pending" if pd.isna(x) else ("$0 (Unpriceable)" if x == 0 else f"${x:,.2f}")
            )

            def color_status(val):
                return 'color: green' if val == 'enriched' else 'color: orange'

            def color_profit(val):
                if "Pending" in str(val) or "Unpriceable" in str(val): return 'color: gray'
                try:
                    return 'color: green' if float(val.replace('$','').replace(',','')) > 0 else 'color: red'
                except: return ''

            st.dataframe(
                display[['id','tx_hash','timestamp','collateral_asset','debt_asset','debt_to_cover',
                          'status','gas_cost_eth','competitor_attempts','quoted_slippage_bps','net_profit_usd']]
                .style.map(color_status, subset=['status']).map(color_profit, subset=['net_profit_usd']),
                use_container_width=True, hide_index=True
            )

except Exception as e:
    st.error(f"Database connection failed: {e}")
    st.caption("Make sure Docker is running: `make up`")
