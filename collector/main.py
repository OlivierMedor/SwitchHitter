import os
import time
import logging
from datetime import datetime
import psycopg2
from psycopg2.extras import execute_values
from web3 import Web3
from dotenv import load_dotenv

# Set up simple logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

RPC_URL = os.getenv("ARBITRUM_RPC_URL")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "switchhitter")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")
DB_NAME = os.getenv("DB_NAME", "switchhitter")

# Arbitrum Aave V3 Pool Contract
AAVE_V3_POOL_ADDRESS = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

# Safe scraping parameters for Free RPC tier 
# Alchemy throws "Log response size exceeded" if the range contains too many events.
# Aave v3 is very busy, so we must use a small batch size.
BLOCKS_PER_BATCH = 10
POLL_INTERVAL_SECONDS = 5

# Minimal ABI for just the LiquidationCall event
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
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )

def setup_sync_state_table(conn):
    """Ensure the sync state table exists to track indexing progress."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS indexer_state (
                id SERIAL PRIMARY KEY,
                indexer_name VARCHAR(50) UNIQUE NOT NULL,
                last_scraped_block BIGINT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

def get_last_scraped_block(conn, start_block):
    """Get the last scraped block from DB. If empty, start from `start_block`."""
    with conn.cursor() as cur:
        cur.execute("SELECT last_scraped_block FROM indexer_state WHERE indexer_name = 'aave_v3_liquidations';")
        result = cur.fetchone()
        if result:
            return result[0]
        else:
            # Initialize it
            cur.execute(
                "INSERT INTO indexer_state (indexer_name, last_scraped_block) VALUES (%s, %s);",
                ('aave_v3_liquidations', start_block)
            )
            conn.commit()
            return start_block

def update_last_scraped_block(conn, block_number):
    """Update the indexer state after a successful batch."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE indexer_state 
            SET last_scraped_block = %s, updated_at = NOW() 
            WHERE indexer_name = 'aave_v3_liquidations';
            """, 
            (block_number,)
        )
        conn.commit()

def process_logs(conn, w3, logs):
    """Process web3 log objects, decode them, and insert into Postgres."""
    if not logs:
        return

    records = []
    
    # We need timestamp for these blocks. 
    # To save RPC calls, we'll cache block timestamps per batch
    block_timestamps = {}
    
    for log in logs:
        # Extract basic tx info
        tx_hash = log['transactionHash'].hex()
        block_number = log['blockNumber']
        
        # Get timestamp
        if block_number not in block_timestamps:
            block = w3.eth.get_block(block_number)
            block_timestamps[block_number] = datetime.fromtimestamp(block['timestamp'])
        
        timestamp = block_timestamps[block_number]
        
        # The args dictionary holds the decoded values
        args = log['args']
        log_index = log['logIndex']
        collateral_asset = args['collateralAsset']
        debt_asset = args['debtAsset']
        user_address = args['user']
        liquidator_address = args['liquidator']
        debt_to_cover = args['debtToCover']
        liquidated_collateral = args['liquidatedCollateralAmount']
        
        records.append((
            tx_hash,
            log_index,
            block_number,
            timestamp,
            collateral_asset,
            debt_asset,
            user_address,
            liquidator_address,
            debt_to_cover,
            liquidated_collateral,
            'raw'
        ))
        
    insert_query = """
        INSERT INTO liquidations (
            tx_hash, log_index, block_number, timestamp, collateral_asset, debt_asset, 
            user_address, liquidator_address, debt_to_cover, liquidated_collateral_amount, status
        ) VALUES %s
        ON CONFLICT (tx_hash, log_index) DO NOTHING;
    """
    
    with conn.cursor() as cur:
        execute_values(cur, insert_query, records)
        conn.commit()
        
    logger.info(f"Inserted {len(records)} events into Postgres.")

def main():
    logger.info(f"Starting Collector using RPC: {RPC_URL[:30]}...")
    
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    try:
        head = w3.eth.block_number
        logger.info(f"Connected to Arbitrum. Current block: {head}")
    except Exception as e:
        logger.error(f"Failed to fetch block number from Arbitrum RPC. Error: {e}")
        # Keep trying in the loop anyway
        
    contract = w3.eth.contract(address=Web3.to_checksum_address(AAVE_V3_POOL_ADDRESS), abi=MINIMAL_ABI)
    
    conn = get_db_connection()
    setup_sync_state_table(conn)
    
    # Optional: Start indexing from 10,000 blocks ago to get immediate sample data
    # (Arbitrum produces roughly ~4 blocks per second, so 10,000 blocks is ~40 mins of history)
    current_head = w3.eth.block_number
    initial_start_block = current_head - 10000 
    
    while True:
        try:
            current_block = w3.eth.block_number
            last_scraped = get_last_scraped_block(conn, initial_start_block)
            
            from_block = last_scraped + 1
            
            # Ensure we never request more than BLOCKS_PER_BATCH blocks in a single RPC call
            to_block = min(from_block + BLOCKS_PER_BATCH - 1, current_block)
            
            if from_block > to_block:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
                
            logger.info(f"Scraping blocks {from_block} to {to_block} ...")
            
            # Fetch events statelessly (better for public/free RPCs than create_filter)
            logs = contract.events.LiquidationCall.get_logs(fromBlock=from_block, toBlock=to_block)
            
            if logs:
                logger.info(f"Found {len(logs)} LiquidationCall events.")
                process_logs(conn, w3, logs)
                
            # Update indexer state to move forward
            update_last_scraped_block(conn, to_block)
            
            # Anti-rate-limit sleep for Alchemy free tier (330 CU / sec)
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
