import os
import time
import requests
import psycopg2
from web3 import Web3
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")
RPC_URL = os.getenv("ARBITRUM_RPC_URL")

GRAPH_API_KEY = os.getenv("GRAPH_API_KEY", "99f1abcd819c8c6d716099bcd83d7481")
SUBGRAPH_ID = "4xyasjQeREe7PxnF6wVdobZvCw5mhoHZq3T7guRpuNPf"
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{GRAPH_API_KEY}/subgraphs/id/{SUBGRAPH_ID}"
AAVE_V3_POOL_ADDRESS = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

MINIMAL_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True, "internalType": "address", "name": "collateralAsset", "type": "address"},
        {"indexed": True, "internalType": "address", "name": "debtAsset", "type": "address"},
        {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
        {"indexed": False, "internalType": "uint256", "name": "debtToCover", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "liquidatedCollateralAmount", "type": "uint256"},
        {"indexed": False, "internalType": "address", "name": "liquidator", "type": "address"},
        {"indexed": False, "internalType": "bool", "name": "receiveAToken", "type": "bool"}
    ],
    "name": "LiquidationCall",
    "type": "event"
}]

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME
    )

def fetch_tx_hashes_from_subgraph(skip=0, first=1000):
    query = """
    query GetLiquidations($first: Int!, $skip: Int!) {
      liquidates(first: $first, skip: $skip, orderBy: timestamp, orderDirection: desc) {
        hash
        timestamp
      }
    }
    """
    response = requests.post(SUBGRAPH_URL, json={'query': query, 'variables': {"first": first, "skip": skip}})
    if response.status_code == 200:
        data = response.json()
        if 'data' in data and data['data'] is not None and 'liquidates' in data['data']:
            return data['data']['liquidates']
        else:
            print(f"[-] Subgraph Error Payload: {data}")
    return []

def main():
    print("--- Switch-Hitter: Hybrid Historical Collector ---")
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    contract = w3.eth.contract(address=Web3.to_checksum_address(AAVE_V3_POOL_ADDRESS), abi=MINIMAL_ABI)
    conn = get_db_connection()
    
    batch_size = 1000
    max_records = 1000
    total_inserted = 0
    checked_hashes = set()
    
    for skip in range(0, max_records, batch_size):
        print(f"[*] Fetching records {skip} to {skip + batch_size} from Subgraph...")
        liquidates = fetch_tx_hashes_from_subgraph(skip=skip, first=batch_size)
        if not liquidates:
            break
            
        records_to_insert = []
        
        # We process transactions from TheGraph one by one to fetch receipts
        # To avoid Alchemy rate limits, we add a tiny sleep
        for idx, liq in enumerate(liquidates):
            tx_hash = liq['hash']
            if tx_hash in checked_hashes:
                continue
            checked_hashes.add(tx_hash)
            timestamp = datetime.fromtimestamp(int(liq['timestamp']))
            
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                # Parse logs for LiquidationCall natively via web3.py!
                processed_logs = contract.events.LiquidationCall().process_receipt(receipt)
                
                for log in processed_logs:
                    args = log['args']
                    records_to_insert.append((
                        tx_hash,
                        log['logIndex'],
                        log['blockNumber'],
                        timestamp,
                        args['collateralAsset'],
                        args['debtAsset'],
                        args['user'],
                        args['liquidator'],
                        str(args['debtToCover']),
                        str(args['liquidatedCollateralAmount']),
                        'raw'
                    ))
            except Exception as e:
                print(f"[-] Error fetching/parsing tx {tx_hash}: {e}")
                continue
                
            time.sleep(0.05) # Alchemy block limit per second safety
            
        if records_to_insert:
            insert_query = """
                INSERT INTO liquidations (
                    tx_hash, log_index, block_number, timestamp, collateral_asset, debt_asset, 
                    user_address, liquidator_address, debt_to_cover, liquidated_collateral_amount, status
                ) VALUES %s
                ON CONFLICT (tx_hash, log_index) DO NOTHING;
            """
            try:
                with conn.cursor() as cur:
                    execute_values(cur, insert_query, records_to_insert)
                    conn.commit()
                total_inserted += len(records_to_insert)
                print(f"[+] Banked {len(records_to_insert)} raw liquidation events.")
            except Exception as e:
                print(f"[-] Database Error: {e}")
                conn.rollback()
            
    print(f"\n[DONE] Finished mapping historical TXs! Pushed ~{total_inserted} events to Postgres.")
    conn.close()

if __name__ == "__main__":
    main()
